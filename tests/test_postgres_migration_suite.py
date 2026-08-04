"""Contracts for the P6.7 full-migration orchestrator and redacted report."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.__main__ import main  # noqa: E402
from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.infrastructure.repositories.migration import SnapshotMigrationReport  # noqa: E402
from seo_control.infrastructure.repositories.migration_suite import (  # noqa: E402
    MigrationBatchDefinition,
    PostgresMigrationSuite,
    _safe_batch_report,
)


class FakeSource:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows


class FakeTarget:
    pass


class FakeService:
    def __init__(self, _source: FakeSource, _target: FakeTarget, report: SnapshotMigrationReport) -> None:
        self.report = report

    def migrate(self, *, apply: bool = False) -> SnapshotMigrationReport:
        if not apply:
            raise AssertionError("suite apply must explicitly enable the batch")
        return self.report

    def validate(self) -> SnapshotMigrationReport:
        return self.report


class PostgresMigrationSuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "suite.sqlite3"
        connection = initialize_database(self.database_path)
        with connection:
            project_id = int(connection.execute("INSERT INTO projects(name) VALUES('suite')").lastrowid)
            connection.execute(
                """INSERT INTO project_wordpress_configs(
                       project_id,site_url,username,application_password
                   ) VALUES(?,?,?,?)""",
                (project_id, "https://example.test", "publisher", "must-not-leak"),
            )
        connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def definition(
        name: str,
        events: list[str],
        *,
        valid: bool = True,
        fail_with: Exception | None = None,
    ) -> MigrationBatchDefinition:
        rows = [{"id": 1}, {"id": 2}]

        def source_factory(_path: str | Path) -> FakeSource:
            events.append(f"source:{name}")
            return FakeSource(rows)

        def target_factory(_url: str) -> FakeTarget:
            events.append(f"target:{name}")
            if fail_with is not None:
                raise fail_with
            return FakeTarget()

        report = SnapshotMigrationReport(
            2,
            2 if valid else 1,
            2 if valid else 0,
            () if valid else (f"{name}:2",),
            (),
            (),
            True,
        )

        def service_factory(source: FakeSource, target: FakeTarget) -> FakeService:
            events.append(f"service:{name}")
            return FakeService(source, target, report)

        return MigrationBatchDefinition(name, source_factory, target_factory, service_factory, lambda source: source.rows)

    def test_preview_reads_only_sources_in_dependency_order_without_database_url(self) -> None:
        events: list[str] = []
        definitions = [self.definition("first", events), self.definition("second", events)]
        report = PostgresMigrationSuite(self.database_path, definitions=definitions).preview()
        self.assertEqual(["source:first", "source:second"], events)
        self.assertEqual(4, report.source_count)
        self.assertIsNone(report.valid)
        self.assertEqual(1, report.credential_actions["wordpress_reauthorization"])
        self.assertNotIn("must-not-leak", json.dumps(report.as_dict()))

    def test_apply_requires_confirmation_and_runs_batches_in_order(self) -> None:
        events: list[str] = []
        definitions = [self.definition("first", events), self.definition("second", events)]
        suite = PostgresMigrationSuite(
            self.database_path,
            "postgresql://user:secret@localhost/rehearsal",
            definitions=definitions,
        )
        with self.assertRaisesRegex(ValueError, "explicit confirmation"):
            suite.migrate(apply=True)
        report = suite.migrate(apply=True, confirmed=True)
        self.assertTrue(report.valid)
        self.assertEqual(
            ["source:first", "target:first", "service:first", "source:second", "target:second", "service:second"],
            events,
        )
        serialized = json.dumps(report.as_dict())
        self.assertNotIn("secret", serialized)
        self.assertEqual("rehearsal", report.target["database"])

    def test_invalid_batch_stops_later_batches(self) -> None:
        events: list[str] = []
        definitions = [
            self.definition("first", events),
            self.definition("broken", events, valid=False),
            self.definition("never", events),
        ]
        report = PostgresMigrationSuite(
            self.database_path, "postgresql://localhost/rehearsal", definitions=definitions
        ).migrate(apply=True, confirmed=True)
        self.assertFalse(report.valid)
        self.assertEqual("broken", report.stopped_after)
        self.assertEqual("not_run", report.batches[-1]["status"])
        self.assertFalse(any(event.endswith(":never") for event in events))

    def test_errors_and_mismatch_values_are_redacted(self) -> None:
        raw = SnapshotMigrationReport(
            1, 1, 0, (), (),
            ({"key": "draft:1", "fields": {"markdown": {"source": "private body", "target": "wrong"}}},),
            False,
        )
        safe = _safe_batch_report("workflow", raw)
        self.assertEqual([{"key": "draft:1", "fields": ["markdown"]}], safe["mismatches"])
        self.assertNotIn("private body", repr(safe))

        events: list[str] = []
        url = "postgresql://user:top-secret@localhost/rehearsal"
        definitions = [self.definition("broken", events, fail_with=ConnectionError(f"failed {url}"))]
        report = PostgresMigrationSuite(self.database_path, url, definitions=definitions).migrate(
            apply=True, confirmed=True
        )
        self.assertNotIn("top-secret", json.dumps(report.as_dict()))
        self.assertIn("[DATABASE_URL]", report.batches[0]["error"])

    def test_cli_preview_needs_no_postgres_and_apply_needs_second_confirmation(self) -> None:
        with redirect_stdout(StringIO()) as output:
            result = main(["migrate-all-postgres", "--database", str(self.database_path)])
        self.assertEqual(0, result)
        self.assertEqual("preview", json.loads(output.getvalue())["mode"])
        with self.assertRaisesRegex(SystemExit, "confirm-full-migration"):
            main([
                "migrate-all-postgres", "--database", str(self.database_path),
                "--database-url", "postgresql://localhost/rehearsal", "--apply",
            ])


if __name__ == "__main__":
    unittest.main()
