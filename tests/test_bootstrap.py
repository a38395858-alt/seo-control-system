"""End-to-end tests for the minimal command-line bootstrap."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"


class BootstrapCommandTests(unittest.TestCase):
    def test_init_db_command_creates_core_sqlite_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "seo-control.sqlite3"
            environment = os.environ.copy()
            existing_pythonpath = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                str(SRC_ROOT)
                if not existing_pythonpath
                else f"{SRC_ROOT}{os.pathsep}{existing_pythonpath}"
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "seo_control",
                    "init-db",
                    "--database",
                    str(database_path),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(database_path.is_file())
            connection = sqlite3.connect(database_path)
            try:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                connection.close()
            self.assertTrue({"projects", "keywords"}.issubset(table_names))
