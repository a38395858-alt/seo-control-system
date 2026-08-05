"""Fast protocol collection contracts for competitor content pages."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import requests

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.browser_competitor_content_client import _ArticleTextParser, _FallbackArticleTextParser, _LooseArticleTextParser, _unwrap_bing_result_url, BrowserCompetitorContentClient  # noqa: E402
from seo_control.web import competitor_bing_query, competitor_search_queries  # noqa: E402


class CompetitorContentClientTests(unittest.TestCase):
    def test_competitor_query_keeps_full_title_and_adds_intent_focused_waterproof_variant(self) -> None:
        queries = competitor_search_queries(
            "Are LED Stair Lights Outdoor Solar Waterproof? What to Look For",
            "led stair lights outdoor solar",
        )
        self.assertEqual("Are LED Stair Lights Outdoor Solar Waterproof? What to Look For", queries[0])
        self.assertIn("waterproof", queries[1].casefold())
        self.assertIn("ip rating", queries[1].casefold())
        self.assertNotIn("what to look for", queries[1].casefold())

    def test_inspiration_title_uses_the_full_title_before_the_keyword_variant(self) -> None:
        title = "10 Inspiring Ideas for LED Stair Lights Outdoor"
        queries = competitor_search_queries(title, "led stair lights outdoor")

        self.assertEqual(title, queries[0])
        self.assertEqual("led stair lights outdoor ideas guide", queries[1])

    def test_bing_uses_a_natural_outdoor_stair_lighting_ideas_query(self) -> None:
        query = competitor_bing_query(
            "10 Inspiring Ideas for LED Stair Lights Outdoor",
            "led stair lights outdoor",
        )

        self.assertEqual("outdoor stair lighting ideas guide", query)

    def test_unwraps_bing_redirect_to_the_actual_editorial_page(self) -> None:
        redirect = "https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9vdXRkb29yLXN0YWlyLWxpZ2h0aW5n"

        self.assertEqual("https://example.com/outdoor-stair-lighting", _unwrap_bing_result_url(redirect))

    def test_fallback_parser_recovers_article_text_from_an_unbalanced_header_template(self) -> None:
        html = "<header><p>Navigation text that should not hide the article.</p><main><h1>IP ratings</h1><p>" + ("Article evidence about waterproof LED strip lights. " * 25) + "</p><p>" + ("A second useful paragraph for the buyer decision. " * 15) + "</p></main>"
        strict = _ArticleTextParser(); strict.feed(html)
        fallback = _FallbackArticleTextParser(); fallback.feed(html)
        self.assertEqual([], strict.blocks)
        self.assertGreaterEqual(len(fallback.blocks), 3)
        self.assertGreater(len("\n".join(fallback.blocks)), 500)

    def test_loose_parser_recovers_div_only_editorial_text_after_semantic_parsers_fail(self) -> None:
        article = "<div>" + ("Outdoor stair lighting ideas explain safe placement, fixture spacing, and visual hierarchy. " * 24) + "</div>"
        strict = _ArticleTextParser(); strict.feed(article)
        fallback = _FallbackArticleTextParser(); fallback.feed(article)
        loose = _LooseArticleTextParser(); loose.feed(article)
        self.assertEqual([], strict.blocks)
        self.assertEqual([], fallback.blocks)
        self.assertGreater(len("\n".join(loose.blocks)), 500)

    def test_protocol_page_collection_runs_independent_urls_concurrently(self) -> None:
        client = BrowserCompetitorContentClient(browser=object(), max_workers=5)  # type: ignore[arg-type]
        active = 0; peak = 0; lock = threading.Lock()

        def extract(*, url: str) -> dict[str, str]:
            nonlocal active, peak
            with lock:
                active += 1; peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return {"title": url, "content": "article text " * 80, "domain": "example.test"}

        client.extract = extract  # type: ignore[method-assign]
        urls = [f"https://example-{number}.test/article" for number in range(5)]
        result = client.extract_many(urls)
        self.assertEqual(set(urls), set(result))
        self.assertGreaterEqual(peak, 2)

    def test_protocol_collection_keeps_individual_failures_for_run_logs(self) -> None:
        client = BrowserCompetitorContentClient(browser=object())  # type: ignore[arg-type]
        def extract(*, url: str) -> dict[str, str]:
            if url.endswith("blocked"): raise RuntimeError("robots blocked")
            return {"title": url, "content": "article text " * 80, "domain": "example.test"}
        client.extract = extract  # type: ignore[method-assign]
        result = client.extract_many(["https://example.test/ok", "https://example.test/blocked"])
        self.assertIsInstance(result["https://example.test/blocked"], Exception)
        self.assertIsInstance(result["https://example.test/ok"], dict)

    def test_robots_disallowed_page_is_never_requested_or_rendered(self) -> None:
        client = BrowserCompetitorContentClient(browser=object())  # type: ignore[arg-type]
        with patch("seo_control.application.browser_competitor_content_client._robots_allows", return_value=False), patch.object(client, "_fetch_static_html") as fetch:
            with self.assertRaisesRegex(Exception, "blocked by robots"):
                client.extract(url="https://blocked.example/article")
        fetch.assert_not_called()

    @patch("seo_control.application.browser_competitor_content_client.time.sleep")
    @patch("seo_control.application.browser_competitor_content_client.requests.get")
    def test_static_fetch_retries_with_short_lived_browser_like_headers(self, mock_get, _mock_sleep) -> None:
        first = Mock(); first.raise_for_status.side_effect = requests.ConnectionError("TLS reset")
        second = Mock(); second.raise_for_status.return_value = None
        second.headers = {"Content-Type": "text/html; charset=utf-8"}
        second.content = b"<html><title>Guide</title><p>" + (b"Useful outdoor LED guidance. " * 40) + b"</p></html>"
        second.encoding = "utf-8"
        mock_get.side_effect = [first, second]
        client = BrowserCompetitorContentClient(browser=object())  # type: ignore[arg-type]

        html = client._fetch_static_html("https://example.test/guide")

        self.assertIn("Useful outdoor LED guidance", html)
        self.assertEqual(2, mock_get.call_count)
        self.assertEqual("close", mock_get.call_args.kwargs["headers"]["Connection"])
        self.assertEqual((8, 20), mock_get.call_args.kwargs["timeout"])


if __name__ == "__main__": unittest.main()
