"""Contract tests for the first-phase keyword-research SQLite schema.

These tests intentionally define the smallest public database boundary needed by
the application: ``initialize_database(path)`` returns a ready-to-use SQLite
connection after applying all migrations.  Keep migration details internal to
``seo_control.infrastructure.database``.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.infrastructure.database import initialize_database


class DatabaseSchemaTests(unittest.TestCase):
    """The schema guarantees required by the keyword-research MVP."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "keywords.db"
        self.connection = initialize_database(self.database_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_initialize_creates_the_keyword_research_tables(self) -> None:
        expected_tables = {
            "projects",
            "keyword_research_tasks",
            "keywords",
            "keyword_sources",
            "keyword_metric_snapshots",
            "suggest_query_jobs",
            "import_batches",
        }

        actual_tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        self.assertTrue(
            expected_tables.issubset(actual_tables),
            f"Missing required tables: {sorted(expected_tables - actual_tables)}",
        )

    def test_initialize_is_idempotent_and_applies_schema_to_an_existing_database(
        self,
    ) -> None:
        self.connection.close()
        reopened_connection = initialize_database(self.database_path)
        try:
            table_names = {
                row[0]
                for row in reopened_connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            reopened_connection.close()

        self.assertIn("keywords", table_names)
        self.assertIn("projects", table_names)

    def test_keywords_has_the_market_scoped_unique_constraint(self) -> None:
        expected_columns = (
            "project_id",
            "normalized_keyword",
            "country_code",
            "language_code",
        )

        unique_indexes = self.connection.execute("PRAGMA index_list('keywords')").fetchall()
        unique_index_columns = []
        for index in unique_indexes:
            # SQLite PRAGMA index_list columns: sequence, name, unique, origin, partial.
            if index[2] != 1:
                continue
            index_name = index[1].replace('"', '""')
            columns = tuple(
                row[2]
                for row in self.connection.execute(
                    f'PRAGMA index_info("{index_name}")'
                )
            )
            unique_index_columns.append(columns)

        self.assertIn(expected_columns, unique_index_columns)

    def test_foreign_key_enforcement_is_enabled_and_core_relations_are_declared(
        self,
    ) -> None:
        foreign_keys_enabled = self.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(1, foreign_keys_enabled)

        expected_parent_tables = {
            "keyword_research_tasks": "projects",
            "keywords": "projects",
            "keyword_sources": "keywords",
            "keyword_metric_snapshots": "keywords",
            "suggest_query_jobs": "keyword_research_tasks",
            "import_batches": "projects",
        }
        for child_table, parent_table in expected_parent_tables.items():
            declared_parents = {
                row[2]
                for row in self.connection.execute(
                    f"PRAGMA foreign_key_list('{child_table}')"
                )
            }
            self.assertIn(
                parent_table,
                declared_parents,
                f"{child_table} must reference {parent_table}",
            )


if __name__ == "__main__":
    unittest.main()
