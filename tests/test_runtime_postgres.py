"""Exact PostgreSQL runtime-schema and SQL compatibility contracts."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.infrastructure.runtime_postgres import (  # noqa: E402
    PostgresRuntimeMigration,
    RuntimeSqlTranslator,
    RuntimeRow,
    inspect_sqlite_runtime_schema,
    runtime_index_ddl,
    runtime_table_ddl,
)


class RuntimePostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "runtime.sqlite3"
        connection = initialize_database(self.database_path)
        with connection:
            project_id = int(connection.execute(
                "INSERT INTO projects(name,site_url) VALUES(?,?)", ("Runtime", "https://runtime.test")
            ).lastrowid)
            connection.execute(
                "INSERT INTO project_wordpress_configs(project_id,site_url,username,application_password) VALUES(?,?,?,?)",
                (project_id, "https://runtime.test", "editor", "never-copy-this"),
            )
            connection.execute(
                "INSERT INTO gsc_oauth_connection(id,refresh_token) VALUES(1,?)", ("never-copy-token",)
            )
        connection.close()
        self.tables = inspect_sqlite_runtime_schema(self.database_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_introspection_covers_all_business_tables_and_constraints(self) -> None:
        by_name = {table.name: table for table in self.tables}
        self.assertGreaterEqual(len(by_name), 60)
        self.assertNotIn("schema_migrations", by_name)
        self.assertEqual(("id",), by_name["content_assets"].primary_key)
        self.assertIn(
            ("project_id", "selected_title_candidate_id"),
            [index.columns for index in by_name["content_assets"].indexes if index.unique],
        )
        ddl = runtime_table_ddl(by_name["content_assets"])
        self.assertIn('"seo_workspace_runtime"."content_assets"', ddl)
        self.assertIn("PRIMARY KEY", ddl)
        index = next(item for item in by_name["durable_task_queue"].indexes if item.predicate)
        self.assertIn("WHERE", runtime_index_ddl(by_name["durable_task_queue"], index))

    def test_preview_counts_credentials_without_disclosing_values(self) -> None:
        migration = PostgresRuntimeMigration(
            self.database_path, "postgresql://user:password@localhost/isolated"
        )
        report = migration.preview()
        self.assertEqual(1, report.credential_actions["wordpress_reauthorization"])
        self.assertEqual(1, report.credential_actions["gsc_oauth_reauthorization"])
        gsc = next(item for item in report.tables if item["name"] == "gsc_oauth_connection")
        self.assertEqual(0, gsc["source_count"])
        self.assertNotIn("never-copy", repr(report.as_dict()))

    def test_sql_translator_handles_workspace_sqlite_dialect(self) -> None:
        translator = RuntimeSqlTranslator(self.tables)
        ignored = translator.translate(
            "INSERT OR IGNORE INTO content_authority_source_links(project_id,content_asset_id,authority_source_id) VALUES(?,?,?)"
        )
        self.assertIn("ON CONFLICT DO NOTHING", ignored)
        self.assertEqual(3, ignored.count("%s"))

        replaced = translator.translate(
            "INSERT OR REPLACE INTO keyword_category_assignments(keyword_id,category_id,source,confidence) VALUES(?,?,?,?)"
        )
        self.assertIn("ON CONFLICT(keyword_id,category_id) DO UPDATE", replaced)
        self.assertIn("source=excluded.source", replaced)

        scheduled = translator.translate(
            "UPDATE competitor_learning_schedules SET next_run_at=datetime('now','+' || interval_days || ' days') WHERE id=?"
        )
        self.assertIn("CURRENT_TIMESTAMP", scheduled)
        self.assertIn("::interval", scheduled)
        self.assertNotIn("?", scheduled)

        lease = translator.translate("UPDATE durable_task_queue SET lease_expires_at=datetime('now','+35 minutes') WHERE id=?")
        self.assertIn("INTERVAL '35 minutes'", lease)
        retry = translator.translate("UPDATE durable_task_queue SET available_at=datetime('now','+' || ? || ' minutes') WHERE id=?")
        self.assertIn("(%s || ' minutes')::interval", retry)

        ordered = translator.translate("SELECT * FROM keywords ORDER BY keywords.keyword COLLATE NOCASE")
        self.assertIn("LOWER(keywords.keyword)", ordered)

        age = translator.translate("SELECT CAST(julianday('now') - julianday(?) AS INTEGER) AS age_days")
        self.assertIn("EXTRACT(EPOCH", age)
        self.assertIn("%s", age)

        claimed = translator.translate(
            "UPDATE durable_task_queue SET started_at=COALESCE(started_at,CURRENT_TIMESTAMP) WHERE id=?"
        )
        self.assertIn("COALESCE(started_at,to_char(CURRENT_TIMESTAMP", claimed)
        self.assertIn("WHERE id=%s", claimed)

    def test_runtime_row_supports_mapping_and_sqlite_positional_access(self) -> None:
        row = RuntimeRow({"id": 7, "name": "Runtime"})
        self.assertEqual(7, row["id"])
        self.assertEqual(7, row[0])
        self.assertEqual({"id": 7, "name": "Runtime"}, dict(row))


if __name__ == "__main__":
    unittest.main()
