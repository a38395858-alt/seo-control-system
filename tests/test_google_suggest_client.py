"""Protocol tests for Google autocomplete responses without real network calls."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.google_suggest_client import (
    GoogleSuggestClient,
    GoogleSuggestProtocolError,
)


class GoogleSuggestClientTests(unittest.TestCase):
    def test_fetch_builds_firefox_protocol_url_with_market_parameters(self) -> None:
        requested_urls: list[str] = []
        client = GoogleSuggestClient(
            fetch_json=lambda url: requested_urls.append(url) or ["seo tools", []]
        )

        self.assertEqual([], client.fetch("seo tools", hl="zh-CN", gl="CN"))
        self.assertEqual(
            [
                "https://suggestqueries.google.com/complete/search?client=firefox"
                "&q=seo+tools&hl=zh-CN&gl=CN"
            ],
            requested_urls,
        )

    def test_fetch_parses_and_deduplicates_common_response(self) -> None:
        client = GoogleSuggestClient(
            fetch_json=lambda _: [
                "seo tools",
                ["seo tools", "seo tools free", "", "seo tools free", "seo tools online"],
            ]
        )
        self.assertEqual(
            ["seo tools", "seo tools free", "seo tools online"],
            client.fetch("seo tools", hl="en", gl="US"),
        )

    def test_fetch_rejects_unexpected_response_shapes(self) -> None:
        for response in (None, {}, [], ["seo tools"], ["seo tools", "not-a-list"], ["seo tools", ["valid", 42]]):
            with self.subTest(response=response):
                client = GoogleSuggestClient(fetch_json=lambda _: response)
                with self.assertRaises(GoogleSuggestProtocolError):
                    client.fetch("seo tools", hl="en", gl="US")

    def test_fetch_retries_transient_network_failures_before_returning_suggestions(self) -> None:
        attempts = 0
        delays: list[float] = []

        def fetch_json(_: str) -> object:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise GoogleSuggestProtocolError(
                    "Google Suggest network connection failed.",
                    error_code="network_error",
                )
            return ["iphone battery capacity", ["iphone battery capacity mah"]]

        client = GoogleSuggestClient(fetch_json=fetch_json, sleep=delays.append)

        self.assertEqual(
            ["iphone battery capacity mah"],
            client.fetch("iphone battery capacity", hl="en", gl="GB"),
        )
        self.assertEqual(3, attempts)
        self.assertEqual([0.35, 0.7], delays)

    def test_fetch_does_not_retry_non_transient_protocol_errors(self) -> None:
        attempts = 0

        def fetch_json(_: str) -> object:
            nonlocal attempts
            attempts += 1
            raise GoogleSuggestProtocolError("Bad response", error_code="decode_error")

        client = GoogleSuggestClient(fetch_json=fetch_json, sleep=lambda _: None)

        with self.assertRaises(GoogleSuggestProtocolError):
            client.fetch("seo tools", hl="en", gl="US")
        self.assertEqual(1, attempts)
