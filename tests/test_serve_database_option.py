"""CLI contract for choosing the database used by the local web server."""

from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.__main__ import build_parser, main  # noqa: E402


class InvalidMigrationReport:
    valid = False

    @staticmethod
    def as_dict() -> dict:
        return {"valid": False}


class InvalidMigrationService:
    def __init__(self, _source: object, _target: object) -> None:
        pass

    def validate(self) -> InvalidMigrationReport:
        return InvalidMigrationReport()


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

    def test_serve_accepts_runtime_shadow_configuration(self) -> None:
        arguments = build_parser().parse_args([
            "serve", "--database-mode", "shadow",
            "--database-url", "postgresql://localhost/rehearsal",
            "--runtime-state", "data/runtime-state.json",
        ])
        self.assertEqual("shadow", arguments.database_mode)
        self.assertEqual("postgresql://localhost/rehearsal", arguments.database_url)
        self.assertEqual("data/runtime-state.json", arguments.runtime_state)

    def test_postgres_validation_returns_nonzero_for_differences_without_apply_attribute(self) -> None:
        with patch("seo_control.__main__.ProjectMigrationService", InvalidMigrationService), redirect_stdout(StringIO()):
            result = main(
                [
                    "validate-projects-postgres",
                    "--database",
                    "data/test.sqlite3",
                    "--database-url",
                    "postgresql://redacted",
                ]
            )
        self.assertEqual(2, result)

    def test_runtime_postgres_preview_needs_no_live_postgres(self) -> None:
        with redirect_stdout(StringIO()) as output:
            result = main([
                "migrate-runtime-postgres", "--database", "data/seo-control.sqlite3"
            ])
        self.assertEqual(0, result)
        self.assertIn('"mode": "preview"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
