"""One-shot, robots-compliant Scrapy worker for competitor research batches.

The parent web server invokes this module in a separate Python process.  It
has no database access and emits only JSON on stdout, so a slow or failed
crawl cannot take down the application server or leak content across projects.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        urls = [str(value) for value in request.get("urls", []) if isinstance(value, str) and value.startswith(("http://", "https://"))]
        timeout = max(3, min(int(request.get("timeout", 8)), 30))
        max_bytes = max(100_000, min(int(request.get("max_bytes", 2_500_000)), 5_000_000))
    except (TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps({"items": []}))
        return 2
    if not urls:
        print(json.dumps({"items": []}))
        return 0

    import scrapy
    from scrapy.crawler import CrawlerProcess

    items: list[dict[str, str]] = []

    class CompetitorArticleSpider(scrapy.Spider):
        name = "seo_competitor_article"
        custom_settings = {
            "ROBOTSTXT_OBEY": True,
            "ROBOTSTXT_USER_AGENT": "SEOContentResearchBot",
            "USER_AGENT": "SEOContentResearchBot/1.0 (+local-content-research)",
            "CONCURRENT_REQUESTS": min(5, len(urls)),
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "DOWNLOAD_DELAY": 0.4,
            "AUTOTHROTTLE_ENABLED": True,
            "AUTOTHROTTLE_START_DELAY": 0.4,
            "AUTOTHROTTLE_MAX_DELAY": 3.0,
            "DOWNLOAD_TIMEOUT": timeout,
            "DOWNLOAD_MAXSIZE": max_bytes,
            "REDIRECT_MAX_TIMES": 3,
            "LOG_ENABLED": False,
            "TELNETCONSOLE_ENABLED": False,
            "RETRY_ENABLED": False,
        }

        async def start(self):  # type: ignore[no-untyped-def]
            """Scrapy 2.17 uses the async ``start`` entry point."""
            for url in urls:
                yield scrapy.Request(url, callback=self.parse_article, errback=self.capture_error, dont_filter=True, meta={"source_url": url})

        def parse_article(self, response: Any) -> None:
            source_url = str(response.meta.get("source_url") or response.url)
            content_type = response.headers.get(b"Content-Type", b"").decode("latin-1", errors="replace").split(";", 1)[0].casefold()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                items.append({"url": source_url, "status": "failed", "error": "Competitor URL is not an HTML content page."})
                return
            items.append({"url": source_url, "status": "ok", "final_url": response.url, "html": response.text[:max_bytes]})

        def capture_error(self, failure: Any) -> None:
            source_url = str(failure.request.meta.get("source_url") or failure.request.url)
            message = str(failure.value)
            if "robots" in message.casefold():
                message = "Competitor page is blocked by robots.txt."
            items.append({"url": source_url, "status": "failed", "error": message[:300]})

    process = CrawlerProcess({"LOG_ENABLED": False})
    process.crawl(CompetitorArticleSpider)
    process.start(stop_after_crawl=True)
    seen = {item["url"] for item in items}
    items.extend({"url": url, "status": "failed", "error": "Crawler returned no response for this URL."} for url in urls if url not in seen)
    print(json.dumps({"items": items}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
