"""P6.8 runtime source control, shadow reads, gates, and rollback contracts."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.infrastructure.repositories.shadow import (  # noqa: E402
    ShadowProjectRepository,
    compare_rows,
)
from seo_control.infrastructure.runtime_database import (  # noqa: E402
    RuntimeDatabaseController,
    RuntimeDatabaseMode,
)
from seo_control.web import create_server  # noqa: E402


class MemoryProjects:
    def __init__(self, rows=None, *, failure: Exception | None = None) -> None:
        self.rows = deepcopy(rows or [])
        self.failure = failure

    def list_projects(self):
        if self.failure:
            raise self.failure
        return deepcopy(self.rows)

    def list_project_summaries(self):
        return self.list_projects()

    def create_project(self, **fields):
        row = {"id": len(self.rows) + 1, **fields}
        self.rows.append(row)
        return deepcopy(row)

    def update_project(self, project_id, fields):
        row = next(item for item in self.rows if item["id"] == project_id)
        row.update(fields)
        return deepcopy(row)

    def delete_project(self, project_id):
        self.rows = [item for item in self.rows if item["id"] != project_id]


class RuntimeDatabaseCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database_path = root / "runtime.sqlite3"
        self.state_path = root / "runtime-state.json"
        connection = initialize_database(self.database_path)
        connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def controller(self, **kwargs):
        return RuntimeDatabaseController(
            self.database_path,
            database_url="postgresql://runtime-user:super-secret@localhost/rehearsal",
            state_path=self.state_path,
            **kwargs,
        )

    def test_default_is_sqlite_and_database_url_is_redacted(self) -> None:
        controller = self.controller()
        status = controller.status()
        self.assertEqual(RuntimeDatabaseMode.SQLITE, controller.mode)
        serialized = json.dumps(status)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("runtime-user", serialized)
        self.assertEqual("rehearsal", status["postgres_target"]["database"])

    def test_postgres_requested_at_startup_cannot_bypass_cutover(self) -> None:
        controller = self.controller(requested_mode="postgres")
        self.assertEqual(RuntimeDatabaseMode.SQLITE, controller.mode)
        self.assertIn("ignored", controller.status()["startup_warning"])

    def test_hand_edited_postgres_state_cannot_bypass_gate(self) -> None:
        self.state_path.write_text(json.dumps({"mode": "postgres", "shadow": {}}), encoding="utf-8")
        controller = self.controller()
        self.assertEqual(RuntimeDatabaseMode.SQLITE, controller.mode)
        self.assertIn("unapproved", controller.status()["startup_warning"])

    def test_shadow_returns_sqlite_and_records_only_keys_and_field_names(self) -> None:
        source = [{"id": 1, "name": "Private source", "updated_at": "2026-08-03 00:00:00"}]
        target = [{"id": 1, "name": "Wrong private value", "updated_at": "2026-08-03T00:00:00+00:00"}]
        controller = self.controller()
        controller.enable_shadow(confirmation="ENABLE_SHADOW_READS")
        repository = ShadowProjectRepository(MemoryProjects(source), MemoryProjects(target), controller)

        self.assertEqual(source, repository.list_projects())
        status = controller.status()
        self.assertEqual(1, status["shadow"]["failed_checks"])
        serialized = json.dumps(status)
        self.assertIn("name", serialized)
        self.assertNotIn("Private source", serialized)
        self.assertNotIn("Wrong private value", serialized)
        self.assertNotIn("super-secret", self.state_path.read_text(encoding="utf-8"))

    def test_postgres_shadow_failure_never_fails_primary_read(self) -> None:
        rows = [{"id": 3, "name": "SQLite stays available"}]
        controller = self.controller()
        controller.enable_shadow(confirmation="ENABLE_SHADOW_READS")
        repository = ShadowProjectRepository(
            MemoryProjects(rows),
            MemoryProjects(failure=ConnectionError("postgresql://user:leak@host/db unavailable")),
            controller,
        )
        self.assertEqual(rows, repository.list_projects())
        serialized = json.dumps(controller.status())
        self.assertNotIn("user:leak", serialized)
        self.assertEqual(1, controller.status()["shadow"]["failed_checks"])

    def test_shadow_mode_writes_only_sqlite(self) -> None:
        controller = self.controller()
        controller.enable_shadow(confirmation="ENABLE_SHADOW_READS")
        sqlite = MemoryProjects()
        postgres = MemoryProjects()
        repository = ShadowProjectRepository(sqlite, postgres, controller)
        repository.create_project(name="SQLite write", site_url="", industry="", country_code="US", language_code="en")
        self.assertEqual(1, len(sqlite.rows))
        self.assertEqual([], postgres.rows)

    def test_cutover_gate_is_closed_when_runtime_coverage_is_not_confirmed(self) -> None:
        class ValidReport:
            valid = True
            source_count = 100
            target_count = 100
            stopped_after = None
            credential_actions = {"wordpress_reauthorization": 0, "gsc_oauth_reauthorization": 0}

        class Suite:
            def __init__(self, *_args): pass
            def validate(self): return ValidReport()

        controller = self.controller(
            required_shadow_passes=1,
            full_repository_coverage=False,
            migration_suite_factory=Suite,
            postgres_health_check=lambda _url: None,
        )
        controller.record_shadow_result(repository="projects", operation="list", matched=True)
        gate = controller.check_cutover()
        self.assertFalse(gate["ready"])
        self.assertFalse(gate["checks"]["full_repository_coverage"]["passed"])
        self.assertIn("runtime_coverage_not_confirmed", gate["checks"]["full_repository_coverage"]["uncovered"])
        with self.assertRaisesRegex(RuntimeError, "gate is not ready"):
            controller.cutover(confirmation="CUTOVER_TO_POSTGRES")

    def test_rollback_is_immediate_and_keeps_postgres_data(self) -> None:
        controller = self.controller()
        controller.enable_shadow(confirmation="ENABLE_SHADOW_READS")
        status = controller.rollback(confirmation="ROLLBACK_TO_SQLITE")
        self.assertEqual("sqlite", status["mode"])
        self.assertTrue(status["last_transition"]["postgres_data_retained"])

    def test_compare_rows_normalizes_equivalent_timestamps(self) -> None:
        self.assertEqual([], compare_rows(
            [{"id": 1, "updated_at": "2026-08-03 10:20:30"}],
            [{"id": 1, "updated_at": "2026-08-03T10:20:30+00:00"}],
        ))

    def test_runtime_status_shadow_and_rollback_http_api(self) -> None:
        server = create_server(
            "127.0.0.1", 0,
            database_path=self.database_path,
            database_url="postgresql://user:api-secret@localhost/rehearsal",
            runtime_state_path=self.state_path,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(f"{root}/api/runtime-database/status", timeout=10) as response:
                status = json.load(response)
            self.assertEqual("sqlite", status["mode"])
            self.assertNotIn("api-secret", json.dumps(status))

            request = Request(
                f"{root}/api/runtime-database/shadow/enable",
                data=json.dumps({"confirmation": "ENABLE_SHADOW_READS"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urlopen(request, timeout=10) as response:
                self.assertEqual("shadow", json.load(response)["mode"])

            request = Request(
                f"{root}/api/runtime-database/rollback",
                data=json.dumps({"confirmation": "ROLLBACK_TO_SQLITE"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urlopen(request, timeout=10) as response:
                self.assertEqual("sqlite", json.load(response)["mode"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
