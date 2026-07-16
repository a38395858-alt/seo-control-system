"""HTTP contract for safe Google Suggest keyword expansion."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.google_suggest_client import GoogleSuggestProtocolError
from seo_control.web import create_server


class FakeSuggestClient:
    def suggest(self, query: str, *, hl: str, gl: str) -> list[str]:
        return {
            "seo tools": ["seo tools free", "seo tools online"],
            "seo tools free": [],
            "seo tools online": [],
        }.get(query, [])


class PartiallyFailingSuggestClient:
    def suggest(self, query: str, *, hl: str, gl: str) -> list[str]:
        if query == "seo tools free":
            raise GoogleSuggestProtocolError("Google Suggest connection timed out.")
        return {
            "seo tools": ["seo tools free", "seo tools online"],
            "seo tools online": [],
        }.get(query, [])


class SuggestExpansionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.server = create_server(
            "127.0.0.1",
            0,
            database_path=Path(self.temporary_directory.name) / "suggest.sqlite3",
            suggest_client=FakeSuggestClient(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def test_expands_seed_keywords_with_the_configured_market_and_safety_limits(self) -> None:
        request = Request(
            self.base_url + "/api/suggest-expansions",
            data=json.dumps(
                {
                    "seed_keywords": ["seo tools"],
                    "hl": "en",
                    "gl": "US",
                    "max_keywords": 20,
                    "max_requests": 20,
                    "max_depth": 2,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(200, response.status)
        self.assertEqual(
            ["seo tools", "seo tools free", "seo tools online"],
            payload["keywords"],
        )
        self.assertEqual(3, payload["requests_made"])
        self.assertEqual("no_new_keywords", payload["stop_reason"])

    def test_returns_partial_results_and_debug_log_when_a_child_query_fails(self) -> None:
        self.server.suggest_client = PartiallyFailingSuggestClient()
        request = Request(
            self.base_url + "/api/suggest-expansions",
            data=json.dumps(
                {
                    "seed_keywords": ["seo tools"],
                    "hl": "en",
                    "gl": "US",
                    "max_keywords": 20,
                    "max_requests": 10,
                    "max_depth": 2,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(200, response.status)
        self.assertEqual(["seo tools", "seo tools free", "seo tools online"], payload["keywords"])
        self.assertEqual("completed_with_errors", payload["stop_reason"])
        self.assertEqual(
            [
                {
                    "level": "error",
                    "event": "google_suggest_failed",
                    "code": "protocol_error",
                    "message": "Google Suggest connection timed out.",
                }
            ],
            payload["debug_logs"],
        )
