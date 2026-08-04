"""Contracts for P6.3 keyword and collection repositories/migrations."""

from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.__main__ import main  # noqa: E402
from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.infrastructure.repositories import (  # noqa: E402
    CollectionMigrationService,
    KeywordMigrationService,
    PostgresCollectionRepository,
    PostgresKeywordRepository,
    SQLiteCollectionRepository,
    SQLiteKeywordRepository,
)


class MemorySnapshotTarget:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.upsert_count = 0
        self.reset_count = 0
        for row in rows or []:
            self._store(row)

    @staticmethod
    def _key(row: Mapping[str, Any]) -> str:
        entity = row.get("entity")
        return f"{entity}:{row['id']}" if entity else str(row["id"])

    def _store(self, row: Mapping[str, Any]) -> None:
        self.rows[self._key(row)] = deepcopy(dict(row))

    def list_migration_snapshots(self) -> list[dict[str, Any]]:
        return [deepcopy(row) for _key, row in sorted(self.rows.items())]

    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        self.upsert_count += 1
        self._store(snapshot)

    def reset_identity(self) -> None:
        self.reset_count += 1


class KeywordCollectionRepositoryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "p63.sqlite3"
        connection = initialize_database(self.database_path)
        connection.row_factory = __import__("sqlite3").Row
        with connection:
            self.first_project = int(connection.execute(
                "INSERT INTO projects(name) VALUES('first')"
            ).lastrowid)
            self.second_project = int(connection.execute(
                "INSERT INTO projects(name) VALUES('second')"
            ).lastrowid)
        connection.close()
        self.keywords = SQLiteKeywordRepository(self.database_path)
        self.collection = SQLiteCollectionRepository(self.database_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seed_keyword(self, project_id: int, text: str) -> int:
        normalized = text.casefold()
        connection = initialize_database(self.database_path)
        try:
            with connection:
                keyword_id = int(connection.execute(
                    """INSERT INTO keywords(
                           project_id,keyword,normalized_keyword,country_code,language_code,demand_estimate,status
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (project_id, text, normalized, "US", "en", 42, "approved"),
                ).lastrowid)
                connection.execute(
                    """INSERT INTO keyword_metric_snapshots(
                           keyword_id,source_type,metric_date,country_code,language_code,
                           average_monthly_searches,competition_level,competition_index
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (keyword_id, "file_import", "2026-07-01", "US", "en", 1200, "HIGH", 81),
                )
                category_id = int(connection.execute(
                    "INSERT INTO keyword_categories(project_id,name,normalized_name) VALUES(?,?,?)",
                    (project_id, "Commercial", "commercial"),
                ).lastrowid)
                connection.execute(
                    "INSERT INTO keyword_category_assignments(keyword_id,category_id,source) VALUES(?,?,'manual')",
                    (keyword_id, category_id),
                )
                connection.execute(
                    """INSERT INTO keyword_reviews(
                           keyword_id,seed_keyword,provider,is_seo_content_fit,same_topic_as_seed,search_intent
                       ) VALUES(?,?,?,1,1,?)""",
                    (keyword_id, text, "rule", "commercial"),
                )
                candidate_id = int(connection.execute(
                    """INSERT INTO keyword_title_candidates(
                           project_id,keyword_id,title,normalized_title,source_type,status
                       ) VALUES(?,?,?,?,?,'selected')""",
                    (project_id, keyword_id, f"{text} Guide", f"{normalized} guide", "manual"),
                ).lastrowid)
                self.assertGreater(candidate_id, 0)
            return keyword_id
        finally:
            connection.close()

    def seed_collection(self, project_id: int) -> tuple[int, int]:
        connection = initialize_database(self.database_path)
        try:
            with connection:
                plan_id = int(connection.execute(
                    """INSERT INTO collection_plans(
                           project_id,source_type,source_value,settings_json,discovered_count
                       ) VALUES(?,?,?,?,?)""",
                    (project_id, "domain", "example.test", '{"depth": 2, "locale": "en-US"}', 3),
                ).lastrowid)
                catalog_id = int(connection.execute(
                    """INSERT INTO competitor_url_catalog(
                           project_id,normalized_url,url,domain,search_title,collection_status,last_rank,last_query
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        project_id,
                        "https://example.test/guide",
                        "https://example.test/guide?utm_source=test",
                        "example.test",
                        "Editorial guide",
                        "queued",
                        1,
                        "site:example.test",
                    ),
                ).lastrowid)
            return plan_id, catalog_id
        finally:
            connection.close()

    def test_keyword_repository_returns_current_aggregates_and_project_scoped_soft_delete(self) -> None:
        first_id = self.seed_keyword(self.first_project, "LED stair lights")
        second_id = self.seed_keyword(self.second_project, "SEO tools")

        first = self.keywords.list_keywords(self.first_project)
        self.assertEqual(1, len(first))
        self.assertEqual(1200, first[0]["search_volume"])
        self.assertEqual("Commercial", first[0]["category"])
        self.assertEqual("commercial", first[0]["search_intent"])
        self.assertEqual("LED stair lights Guide", first[0]["selected_title"])
        self.assertEqual(1, first[0]["title_candidate_count"])

        self.assertEqual(0, self.keywords.soft_delete(self.first_project, keyword_ids=[second_id]))
        self.assertEqual(1, len(self.keywords.list_keywords(self.second_project)))
        self.assertEqual(1, self.keywords.soft_delete(self.first_project, keyword_ids=[first_id]))
        self.assertEqual([], self.keywords.list_keywords(self.first_project))
        self.assertEqual(2, len(self.keywords.list_migration_snapshots()))

    def test_collection_repository_lists_plans_and_catalog_by_project(self) -> None:
        plan_id, catalog_id = self.seed_collection(self.first_project)
        plans = self.collection.list_collection_plans(self.first_project)
        catalog = self.collection.list_catalog(self.first_project)
        self.assertEqual(plan_id, plans[0]["id"])
        self.assertEqual({"depth": 2, "locale": "en-US"}, plans[0]["settings"])
        self.assertEqual(catalog_id, catalog[0]["id"])
        self.assertEqual([], self.collection.list_collection_plans(self.second_project))
        self.assertEqual([], self.collection.list_catalog(self.second_project))
        with self.assertRaisesRegex(ValueError, "project does not exist"):
            self.collection.list_catalog(99999)

    def test_keyword_migration_is_dry_run_safe_idempotent_and_reports_mismatch(self) -> None:
        self.seed_keyword(self.first_project, "LED stair lights")
        target = MemorySnapshotTarget()
        service = KeywordMigrationService(self.keywords, target)  # type: ignore[arg-type]
        preview = service.migrate(apply=False)
        self.assertEqual(1, preview.source_count)
        self.assertEqual(0, target.upsert_count)

        first = service.migrate(apply=True)
        second = service.migrate(apply=True)
        self.assertTrue(first.valid)
        self.assertTrue(second.valid)
        self.assertEqual(1, len(target.rows))
        self.assertEqual(2, target.upsert_count)

        target.rows[next(iter(target.rows))]["keyword"] = "wrong"
        mismatch = service.validate()
        self.assertFalse(mismatch.valid)
        self.assertIn("keyword", mismatch.mismatches[0]["fields"])

    def test_collection_migration_is_idempotent_and_reports_missing_extra_and_json_equivalence(self) -> None:
        self.seed_collection(self.first_project)
        target = MemorySnapshotTarget()
        service = CollectionMigrationService(self.collection, target)  # type: ignore[arg-type]
        first = service.migrate(apply=True)
        second = service.migrate(apply=True)
        self.assertTrue(first.valid)
        self.assertTrue(second.valid)
        self.assertEqual(2, len(target.rows))
        self.assertEqual(4, target.upsert_count)

        plan_key = next(key for key in target.rows if key.startswith("plan:"))
        target.rows[plan_key]["settings_json"] = json.dumps({"locale": "en-US", "depth": 2})
        self.assertTrue(service.validate().valid)

        removed = target.rows.pop(plan_key)
        missing = service.validate()
        self.assertEqual((plan_key,), missing.missing_keys)
        removed["id"] += 100
        target._store(removed)
        extra = service.validate()
        self.assertTrue(extra.extra_keys)

    def test_dry_runs_do_not_connect_to_postgres_and_sqlite_remains_available(self) -> None:
        self.seed_keyword(self.first_project, "LED stair lights")
        self.seed_collection(self.first_project)

        def unavailable(_database_url: str):
            raise ConnectionError("postgres unavailable")

        keyword_target = PostgresKeywordRepository("postgresql://redacted", connection_factory=unavailable)
        collection_target = PostgresCollectionRepository("postgresql://redacted", connection_factory=unavailable)
        self.assertFalse(KeywordMigrationService(self.keywords, keyword_target).migrate(apply=False).applied)
        self.assertFalse(CollectionMigrationService(self.collection, collection_target).migrate(apply=False).applied)
        self.assertEqual(1, len(self.keywords.list_keywords(self.first_project)))
        self.assertEqual(1, len(self.collection.list_catalog(self.first_project)))

    def test_cli_preview_commands_do_not_require_a_live_postgres_server(self) -> None:
        self.seed_keyword(self.first_project, "LED stair lights")
        self.seed_collection(self.first_project)
        for command in ("migrate-keywords-postgres", "migrate-collection-postgres"):
            with self.subTest(command=command), redirect_stdout(StringIO()):
                result = main([
                    command,
                    "--database",
                    str(self.database_path),
                    "--database-url",
                    "postgresql://preview-only",
                ])
            self.assertEqual(0, result)


if __name__ == "__main__":
    unittest.main()
