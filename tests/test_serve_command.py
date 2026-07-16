"""TDD contract for the local web-server command-line interface."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


from seo_control.__main__ import build_parser  # noqa: E402


class ServeCommandParserTests(unittest.TestCase):
    """The CLI exposes a configurable command for the local web interface."""

    def test_serve_command_parses_host_and_port(self) -> None:
        arguments = build_parser().parse_args(
            ["serve", "--host", "127.0.0.1", "--port", "8080"]
        )

        self.assertEqual("serve", arguments.command)
        self.assertEqual("127.0.0.1", arguments.host)
        self.assertEqual(8080, arguments.port)

