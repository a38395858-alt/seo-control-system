"""Fast protocol collection contracts for competitor content pages."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.browser_competitor_content_client import _ArticleTextParser, _FallbackArticleTextParser, BrowserCompetitorContentClient  # noqa: E402


class CompetitorContentClientTests(unittest.TestCase):
    def test_fallback_parser_recovers_article_text_from_an_unbalanced_header_template(self) -> None:
        html = "<header><p>Navigation text that should not hide the article.</p><main><h1>IP ratings</h1><p>" + ("Article evidence about waterproof LED strip lights. " * 25) + "</p><p>" + ("A second useful paragraph for the buyer decision. " * 15) + "</p></main>"
        strict = _ArticleTextParser(); strict.feed(html)
        fallback = _FallbackArticleTextParser(); fallback.feed(html)
        self.assertEqual([], strict.blocks)
        self.assertGreaterEqual(len(fallback.blocks), 3)
        self.assertGreater(len("\n".join(fallback.blocks)), 500)

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


if __name__ == "__main__": unittest.main()
