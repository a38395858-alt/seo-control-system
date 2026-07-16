"""Migration contract for per-row keyword-import audit records."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.infrastructure.database import initialize_database


class ImportRowsMigrationTests(unittest.TestCase):
    def test_initialize_database_creates_import_rows_with_batch_foreign_key(self) -> None:
        connection = initialize_database(":memory:")
        self.addCleanup(connection.close)

        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(import_rows)").fetchall()
        }
        self.assertTrue(
            {
                "import_batch_id",
                "row_number",
                "keyword",
                "status",
                "rejection_reason",
            }.issubset(columns)
        )

        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(import_rows)"
        ).fetchall()
        self.assertTrue(
            any(
                foreign_key[2] == "import_batches"
                and foreign_key[3] == "import_batch_id"
                for foreign_key in foreign_keys
            ),
            "import_rows.import_batch_id must reference import_batches(id)",
        )


if __name__ == "__main__":
    unittest.main()
