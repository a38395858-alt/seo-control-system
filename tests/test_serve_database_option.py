"""CLI contract for choosing the database used by the local web server."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.__main__ import build_parser  # noqa: E402


class ServeDatabaseOptionTests(unittest.TestCase):
    def test_serve_accepts_an_explicit_database_path(self) -> None:
        arguments = build_parser().parse_args(
            ["serve", "--database", "data/test.sqlite3"]
        )

        self.assertEqual("serve", arguments.command)
        self.assertEqual("data/test.sqlite3", arguments.database)

    def test_serve_uses_the_local_default_database_path(self) -> None:
        arguments = build_parser().parse_args(["serve"])

        self.assertEqual("serve", arguments.command)
        self.assertEqual(PROJECT_ROOT / "data" / "seo-control.sqlite3", arguments.database)


if __name__ == "__main__":
    unittest.main()
