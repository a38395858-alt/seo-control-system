from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.gsc_browser_capture_client import GscBrowserCaptureClient  # noqa: E402


class GscBrowserCaptureClientTests(unittest.TestCase):
    def test_open_console_always_resets_to_united_states_and_seven_days(self) -> None:
        client = GscBrowserCaptureClient()
        opened: list[str] = []
        client._chrome._ensure_chrome = lambda: None  # type: ignore[method-assign]
        client._navigate = opened.append  # type: ignore[method-assign]

        client.open_console(property_url="https://example.com/")

        self.assertEqual(1, len(opened))
        self.assertIn("country=usa", opened[0])
        self.assertIn("last_7_days=true", opened[0])
        self.assertIn("breakdown=query", opened[0])

    def test_builds_the_same_query_filtered_page_url_used_by_gsc(self) -> None:
        value = GscBrowserCaptureClient._performance_url(
            "https://search.google.com/search-console/performance/search-analytics?resource_id=https%3A%2F%2Fexample.com%2F&breakdown=query&metrics=CLICKS%2CIMPRESSIONS%2CPOSITION",
            breakdown="page",
            query="step lights",
        )

        self.assertIn("breakdown=page", value)
        self.assertIn("query=%21step+lights", value)
        self.assertIn("resource_id=https%3A%2F%2Fexample.com%2F", value)
        self.assertIn("last_7_days=true", value)
        self.assertIn("country=usa", value)
        self.assertNotIn("last_24_hours", value)

    def test_replaces_any_existing_date_filter_with_the_default_seven_days(self) -> None:
        value = GscBrowserCaptureClient._performance_url(
            "https://search.google.com/search-console/performance/search-analytics?last_24_hours=true&num_of_days=28&breakdown=query",
            breakdown="query",
        )

        self.assertIn("last_7_days=true", value)
        self.assertNotIn("last_24_hours", value)
        self.assertNotIn("num_of_days", value)

    def test_replaces_existing_country_filter_with_united_states(self) -> None:
        value = GscBrowserCaptureClient._performance_url(
            "https://search.google.com/search-console/performance/search-analytics?country=chn&breakdown=query",
            breakdown="page",
        )

        self.assertIn("country=usa", value)
        self.assertNotIn("country=chn", value)

    def test_only_all_ascii_letter_queries_are_treated_as_english(self) -> None:
        self.assertTrue(GscBrowserCaptureClient._is_english_query("outdoor LED step lights"))
        self.assertTrue(GscBrowserCaptureClient._is_english_query("IP65 stair light 12v"))
        self.assertFalse(GscBrowserCaptureClient._is_english_query("户外 led step lights"))
        self.assertFalse(GscBrowserCaptureClient._is_english_query("luces de escalera"))
        self.assertFalse(GscBrowserCaptureClient._is_english_query("ступенчатые светильники"))

    def test_parses_query_rows_and_keeps_position_without_an_enabled_ctr_column(self) -> None:
        document = {"rows": [{"cells": [{"text": "step lights", "href": ""}, {"text": "0", "href": ""}, {"text": "4", "href": ""}, {"text": "9.5", "href": ""}]}]}

        rows = GscBrowserCaptureClient()._parse_visible_rows(document, fallback_page_url="https://example.com")

        self.assertEqual(1, len(rows))
        self.assertEqual("step lights", rows[0]["query"])
        self.assertEqual(4, rows[0]["impressions"])
        self.assertEqual(9.5, rows[0]["position"])

    def test_parses_page_rows_without_mistaking_the_page_for_a_query(self) -> None:
        document = {"rows": [{"cells": [{"text": "https://example.com/stair-lights/", "href": ""}, {"text": "1", "href": ""}, {"text": "20", "href": ""}, {"text": "12.8", "href": ""}]}]}

        rows = GscBrowserCaptureClient()._parse_visible_rows(document, fallback_page_url="")

        self.assertEqual("https://example.com/stair-lights/", rows[0]["query"])
        self.assertEqual(12.8, rows[0]["position"])


if __name__ == "__main__":
    unittest.main()
