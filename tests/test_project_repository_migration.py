"""Contracts for the staged workspace-project PostgreSQL migration."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.infrastructure.repositories.projects import (  # noqa: E402
    PostgresProjectRepository,
    ProjectMigrationService,
    SQLiteProjectRepository,
    _timestamp,
)


class MemoryProjectTarget:
    """Materialized-summary target used without requiring a PostgreSQL daemon."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = {int(row["id"]): deepcopy(row) for row in rows or []}
        self.upsert_count = 0
        self.reset_count = 0

    def list_project_summaries(self) -> list[dict[str, Any]]:
        return [deepcopy(row) for _project_id, row in sorted(self.rows.items(), reverse=True)]

    def upsert_project_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        self.upsert_count += 1
        self.rows[int(snapshot["id"])] = deepcopy(dict(snapshot))

    def reset_identity(self) -> None:
        self.reset_count += 1


class ProjectRepositoryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "projects.sqlite3"
        self.repository = SQLiteProjectRepository(self.database_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_project(self, name: str = "Source") -> dict[str, Any]:
        return self.repository.create_project(
            name=name,
            site_url=f"https://{name.lower()}.example",
            industry="lighting",
            country_code="US",
            language_code="en",
        )

    def test_sqlite_repository_crud(self) -> None:
        created = self.create_project()
        self.assertEqual([created["id"]], [row["id"] for row in self.repository.list_projects()])

        updated = self.repository.update_project(
            created["id"],
            {"name": "Updated", "default_country": "CA", "ignored": "not persisted"},
        )
        self.assertEqual("Updated", updated["name"])
        self.assertEqual("CA", updated["default_country"])
        self.assertNotIn("ignored", updated)

        self.repository.delete_project(created["id"])
        self.assertEqual([], self.repository.list_projects())
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.repository.delete_project(created["id"])

    def test_summary_returns_real_child_counts_and_latest_status(self) -> None:
        project = self.create_project()
        connection = initialize_database(self.database_path)
        try:
            with connection:
                keyword_id = connection.execute(
                    """INSERT INTO keywords(project_id,keyword,normalized_keyword,country_code,language_code)
                       VALUES(?,?,?,?,?)""",
                    (project["id"], "LED light", "led light", "US", "en"),
                ).lastrowid
                candidate_id = connection.execute(
                    """INSERT INTO keyword_title_candidates(
                           project_id,keyword_id,title,normalized_title,source_type,status
                       ) VALUES(?,?,?,?,?,?)""",
                    (project["id"], keyword_id, "LED Guide", "led guide", "manual", "selected"),
                ).lastrowid
                connection.execute(
                    """INSERT INTO content_assets(
                           project_id,keyword_id,selected_title_candidate_id,title_snapshot,status
                       ) VALUES(?,?,?,?,?)""",
                    (project["id"], keyword_id, candidate_id, "LED Guide", "approved"),
                )
                connection.execute(
                    """INSERT INTO project_knowledge_documents(project_id,title,content,status)
                       VALUES(?,?,?,?)""",
                    (project["id"], "Brand facts", "Verified facts", "ready"),
                )
        finally:
            connection.close()

        summary = self.repository.list_project_summaries()[0]
        self.assertEqual(1, summary["keyword_count"])
        self.assertEqual(1, summary["selected_title_count"])
        self.assertEqual(1, summary["content_count"])
        self.assertEqual(1, summary["knowledge_count"])
        self.assertEqual("approved", summary["latest_content_status"])

    def test_dry_run_does_not_write_target(self) -> None:
        self.create_project()
        target = MemoryProjectTarget()
        report = ProjectMigrationService(self.repository, target).migrate(apply=False)
        self.assertFalse(report.applied)
        self.assertEqual(1, report.source_count)
        self.assertEqual(0, report.migrated_count)
        self.assertEqual(0, target.upsert_count)
        self.assertEqual(0, target.reset_count)

    def test_apply_is_idempotent_and_validates_without_differences(self) -> None:
        self.create_project("First")
        self.create_project("Second")
        target = MemoryProjectTarget()
        service = ProjectMigrationService(self.repository, target)

        first = service.migrate(apply=True)
        second = service.migrate(apply=True)

        self.assertTrue(first.valid)
        self.assertTrue(second.valid)
        self.assertEqual(2, len(target.rows))
        self.assertEqual(4, target.upsert_count)
        self.assertEqual(2, target.reset_count)

    def test_validation_reports_missing_extra_and_field_mismatches(self) -> None:
        source = self.create_project()
        source_row = self.repository.list_project_summaries()[0]
        changed = deepcopy(source_row)
        changed["name"] = "Wrong target name"
        extra = deepcopy(source_row)
        extra["id"] = source["id"] + 100
        extra["name"] = "Extra"
        target = MemoryProjectTarget([changed, extra])

        report = ProjectMigrationService(self.repository, target).validate()
        self.assertEqual((), report.missing_ids)
        self.assertEqual((source["id"] + 100,), report.extra_ids)
        self.assertEqual(source["id"], report.mismatches[0]["project_id"])
        self.assertIn("name", report.mismatches[0]["fields"])

        target.rows.pop(source["id"])
        report = ProjectMigrationService(self.repository, target).validate()
        self.assertEqual((source["id"],), report.missing_ids)

    def test_timestamp_comparison_normalizes_sqlite_and_postgres_values(self) -> None:
        self.assertEqual("2026-07-31T04:00:00Z", _timestamp("2026-07-31 04:00:00"))
        self.assertEqual("2026-07-31T04:00:00Z", _timestamp("2026-07-31T12:00:00+08:00"))
        self.assertEqual(
            "2026-07-31T04:00:00Z",
            _timestamp(datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc)),
        )

    def test_postgres_connection_failure_does_not_affect_sqlite_repository(self) -> None:
        created = self.create_project()

        def unavailable(_database_url: str):
            raise ConnectionError("postgres unavailable")

        postgres = PostgresProjectRepository("postgresql://redacted", connection_factory=unavailable)
        with self.assertRaisesRegex(ConnectionError, "postgres unavailable"):
            postgres.list_projects()
        self.assertEqual(created["id"], self.repository.list_projects()[0]["id"])


if __name__ == "__main__":
    unittest.main()
