"""Browser-backed Google result and public-page extraction for content research."""

from __future__ import annotations

import base64
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.robotparser import RobotFileParser
from urllib.request import Request, urlopen

import requests

from seo_control.application.browser_serp_title_client import (
    BrowserSerpTitleClient,
    GoogleSerpProtocolError,
    _GoogleResultParser,
)


class CompetitorContentProtocolError(GoogleSerpProtocolError):
    """A public competitor page cannot be safely used as a source."""


def _unwrap_bing_result_url(href: str) -> str:
    """Resolve Bing's ``/ck/a`` redirect without fetching the redirect page."""
    parsed = urlparse(href)
    if parsed.hostname and parsed.hostname.endswith("bing.com") and parsed.path.startswith("/ck/"):
        encoded = parse_qs(parsed.query).get("u", [""])[0]
        if encoded.startswith("a1"):
            try:
                value = encoded[2:]
                decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
                if urlparse(decoded).scheme in {"http", "https"}:
                    return decoded
            except (UnicodeDecodeError, ValueError):
                pass
    return href


def _robots_allows(url: str, *, timeout: int) -> bool | None:
    """Return an explicit robots decision, or ``None`` when it cannot be read.

    ``RobotFileParser.read`` sends Python's default request header.  A number
    of ordinary public sites block that header and return an HTML error page;
    RobotFileParser then treats the failed read as a blanket disallow.  Read
    the robots file using a normal browser identity so an explicit Allow or
    Disallow is evaluated correctly.  An unavailable robots file remains
    unknown (the caller's existing public-page request decides accessibility),
    never a fabricated blanket prohibition.
    """
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    request = Request(
        robots_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SEOContentResearchBot/1.0; +local-content-research)"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - public robots file for a user-supplied public page
            charset = response.headers.get_content_charset() or "utf-8"
            document = response.read(300_000).decode(charset, errors="replace")
    except Exception:
        return None
    robots = RobotFileParser()
    robots.set_url(robots_url)
    robots.parse(document.splitlines())
    return robots.can_fetch("SEOContentResearchBot", url)


class _ArticleTextParser(HTMLParser):
    _ignored = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form"}
    _content = {"p", "li", "h1", "h2", "h3", "h4", "td", "th", "blockquote"}

    def __init__(self) -> None:
        super().__init__()
        self._ignored_depth = 0
        self._active: str | None = None
        self._parts: list[str] = []
        self.title = ""
        self._in_title = False
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._ignored:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True
        if not self._ignored_depth and tag in self._content:
            self._active, self._parts = tag, []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._active is not None and not self._ignored_depth:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if self._active == tag:
            text = " ".join("".join(self._parts).split())
            if len(text) >= 30 and text not in self.blocks:
                self.blocks.append(text)
            self._active, self._parts = None, []
        if tag in self._ignored and self._ignored_depth:
            self._ignored_depth -= 1


class _FallbackArticleTextParser(_ArticleTextParser):
    """Recover readable page text when a site's malformed layout hides main."""

    # Some commerce templates leave header/nav markup unbalanced.  The strict
    # parser correctly avoids chrome on well-formed pages, but would then
    # suppress the entire article.  The AI relevance gate runs after this
    # fallback and rejects navigation/product pages that are not on-topic.
    _ignored = {"script", "style", "noscript", "svg"}


class _LooseArticleTextParser(HTMLParser):
    """Last-resort visible-text extractor for ``div``-only article themes.

    Some editorial WordPress themes render the complete article as plain text
    directly inside nested ``div`` elements.  The semantic parser intentionally
    ignores those containers so it does not mistake a storefront for an
    article, but that also drops legitimate guides such as Lumens' editorial
    pages.  This parser is used only after both semantic extractors fail; the
    existing product-page exclusion and AI relevance gate still decide whether
    the recovered text can become competitor evidence.
    """

    _ignored = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__()
        self._ignored_depth = 0
        self._in_title = False
        self.title = ""
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._ignored:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._ignored_depth:
            return
        text = " ".join(data.split())
        # Navigation labels are normally short.  Keeping substantive text
        # chunks gives div-based articles useful paragraph boundaries while
        # avoiding a noisy dump of individual menu labels.
        if len(text) >= 30 and text not in self.blocks:
            self.blocks.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self._ignored and self._ignored_depth:
            self._ignored_depth -= 1


class _BingResultParser(HTMLParser):
    """Extract ordinary organic result links from Bing's server-rendered HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, str]] = []
        self._in_result = False
        self._in_heading = False
        self._href = ""
        self._title_parts: list[str] = []
        self._capture_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "li" and "b_algo" in (values.get("class") or "").split():
            self._in_result = True
            self._in_heading = False
            self._href, self._title_parts, self._capture_title = "", [], False
            return
        if self._in_result:
            if tag == "h2":
                self._in_heading = True
            elif tag == "a" and self._in_heading and not self._href and values.get("href"):
                self._href = str(values["href"])
                self._capture_title = True

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_result:
            return
        if tag == "a" and self._capture_title:
            self._capture_title = False
        elif tag == "h2":
            self._in_heading = False
        elif tag == "li":
            title = " ".join("".join(self._title_parts).split())
            if self._href and title:
                self.items.append({"title": title, "href": self._href})
            self._in_result = False
            self._in_heading = False


class BrowserCompetitorContentClient:
    """Read Google results then extract accessible public pages compliantly.

    Google is only queried in a visible local Chrome profile.  Competitor
    pages first use the lightweight HTTP stage (the same static-first pattern
    used by Scrapy jobs).  When the optional crawler dependencies are
    installed, Trafilatura extracts clean article text, Playwright is a
    JavaScript-rendering fallback, and Crawl4AI is the last structured-content
    fallback.  Every stage runs only after the robots policy permits it.

    Scrapy remains the recommended scheduler for future site-wide/background
    crawl jobs.  This request/response client deliberately does not start a
    Scrapy reactor for each five-page research job: doing so from a running
    web server is unsafe and slower than bounded concurrent HTTP requests.
    """

    def __init__(self, browser: BrowserSerpTitleClient | None = None, *, max_page_chars: int = 60_000, timeout: int = 8, max_workers: int = 5) -> None:
        self.browser = browser or BrowserSerpTitleClient()
        self.max_page_chars = max_page_chars
        self.timeout = timeout
        self.max_workers = max_workers

    def search(self, *, query: str, locale: str = "en-US", max_results: int = 20) -> list[dict[str, Any]]:
        language, country = (locale.split("-", 1) + ["US"])[:2] if "-" in locale else ("en", "US")
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for start in range(0, min(max_results, 20), 10):
            from urllib.parse import urlencode
            search_url = f"https://www.google.com/search?{urlencode({'q': query, 'hl': language, 'gl': country, 'start': start})}"
            parser = _GoogleResultParser()
            # Google occasionally returns a completed shell before organic
            # result nodes are present. A second DOM read is cheap compared
            # with page extraction and avoids falsely reporting no pages.
            for _attempt in range(2):
                parser = _GoogleResultParser(); parser.feed(self.browser._chrome_dump_dom(search_url))
                if parser.items:
                    break
            for item in parser.items:
                title, url = item.get("title"), item.get("href")
                parsed = urlparse(url) if isinstance(url, str) else None
                if not isinstance(title, str) or not isinstance(url, str) or not parsed or parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    continue
                normalized = url.split("#", 1)[0].rstrip("/").casefold()
                if normalized in seen or parsed.hostname.endswith("google.com"):
                    continue
                seen.add(normalized)
                results.append({"rank": len(results) + 1, "title": " ".join(title.split()), "url": url, "domain": parsed.hostname.removeprefix("www.")})
                if len(results) >= max_results:
                    return results
        if not results:
            raise GoogleSerpProtocolError("Google returned no readable organic results; retry after confirming the visible search page has loaded.")
        return results

    def search_bing(self, *, query: str, locale: str = "en-US", max_results: int = 10) -> list[dict[str, Any]]:
        """Use Bing only as a fallback; returned pages still pass all content gates."""
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        language, country = (locale.split("-", 1) + ["US"])[:2] if "-" in locale else (locale or "en", "US")
        for first in range(1, min(max_results, 20) + 1, 10):
            url = f"https://www.bing.com/search?{urlencode({'q': query, 'first': first, 'setlang': f'{language}-{country}', 'cc': country})}"
            try:
                # Bing's direct HTTP response is often a bot/interstitial page
                # or a wrong-local-market result. Reuse the local Chrome DOM
                # reader only for this fallback so the same user-visible
                # regional results are parsed.
                html = self.browser._chrome_dump_dom(url)
            except Exception as error:
                raise GoogleSerpProtocolError(f"Bing search request failed: {type(error).__name__}.") from error
            parser = _BingResultParser(); parser.feed(html)
            for item in parser.items:
                title, result_url = item["title"], _unwrap_bing_result_url(item["href"])
                parsed = urlparse(result_url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    continue
                normalized = result_url.split("#", 1)[0].rstrip("/").casefold()
                if normalized in seen or parsed.hostname.endswith("bing.com"):
                    continue
                seen.add(normalized)
                results.append({"rank": len(results) + 1, "title": title, "url": result_url, "domain": parsed.hostname.removeprefix("www.")})
                if len(results) >= max_results:
                    return results
        if not results:
            raise GoogleSerpProtocolError("Bing returned no readable organic results.")
        return results

    def extract(self, *, url: str, respect_robots: bool = True) -> dict[str, str]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise CompetitorContentProtocolError("Competitor URL is not a public HTTP(S) page.")
        if respect_robots:
            allowed = _robots_allows(url, timeout=self.timeout)
            if allowed is False:
                raise CompetitorContentProtocolError("Competitor page is blocked by robots.txt.")
        try:
            html = self._fetch_static_html(url)
        except CompetitorContentProtocolError:
            raise
        except Exception as error:
            raise CompetitorContentProtocolError(f"Competitor HTTP fetch failed: {type(error).__name__}.") from error
        title, content, extractor = self._extract_article(html, parsed.hostname)
        if len(content) < 500:
            rendered = self._render_with_playwright(url)
            if rendered:
                rendered_title, rendered_content, rendered_extractor = self._extract_article(rendered, parsed.hostname)
                if len(rendered_content) > len(content):
                    title, content, extractor = rendered_title, rendered_content, rendered_extractor
        if len(content) < 500:
            crawl4ai = self._extract_with_crawl4ai(url)
            if crawl4ai and len(crawl4ai) > len(content):
                content, extractor = crawl4ai[: self.max_page_chars], "crawl4ai"
        if len(content) < 500:
            raise CompetitorContentProtocolError("Competitor page is not a usable article page.")
        return {"title": title or parsed.hostname, "content": content, "domain": parsed.hostname.removeprefix("www."), "extractor": extractor}

    def _fetch_static_html(self, url: str) -> str:
        """Static collection stage used by small research batches.

        This is intentionally cache-free and bounded.  Site-wide recurring
        jobs should use the separate Scrapy scheduler, where AutoThrottle,
        persistent de-duplication and per-domain queues are available.
        """
        response = requests.get(
            url,
            headers={"User-Agent": "SEOContentResearchBot/1.0 (+local-content-research)"},
            timeout=(4, self.timeout),
            allow_redirects=True,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].casefold()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise CompetitorContentProtocolError("Competitor URL is not an HTML content page.")
        return response.content[: max(self.max_page_chars * 12, 2_500_000)].decode(response.encoding or "utf-8", errors="replace")

    def _extract_article(self, html: str, hostname: str) -> tuple[str, str, str]:
        """Use Trafilatura when available, then the dependency-free parser."""
        try:
            import trafilatura

            extracted = trafilatura.extract(html, output_format="txt", include_comments=False, include_tables=True, favor_precision=True)
            metadata = trafilatura.extract_metadata(html)
            text = "\n\n".join(str(extracted or "").splitlines()).strip()[: self.max_page_chars]
            if len(text) >= 500:
                return " ".join(str(getattr(metadata, "title", "") or hostname).split()), text, "trafilatura"
        except (ImportError, ValueError, TypeError):
            pass
        parser = _ArticleTextParser(); parser.feed(html)
        content = "\n\n".join(parser.blocks).strip()[: self.max_page_chars]
        if len(content) < 500 or len(parser.blocks) < 3:
            fallback = _FallbackArticleTextParser(); fallback.feed(html)
            fallback_content = "\n\n".join(fallback.blocks).strip()[: self.max_page_chars]
            if len(fallback_content) > len(content):
                parser, content = fallback, fallback_content
        if len(content) < 500 or len(parser.blocks) < 3:
            loose = _LooseArticleTextParser(); loose.feed(html)
            loose_content = "\n\n".join(loose.blocks).strip()[: self.max_page_chars]
            if len(loose_content) > len(content):
                parser, content = loose, loose_content
        return " ".join(parser.title.split()) or hostname, content, "html-parser"

    def _render_with_playwright(self, url: str) -> str | None:
        """Render a permitted JavaScript article only when static text failed."""
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(user_agent="SEOContentResearchBot/1.0 (+local-content-research)")
                    page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                    page.wait_for_timeout(350)
                    return page.content()
                finally:
                    browser.close()
        except Exception:
            return None

    def _extract_with_crawl4ai(self, url: str) -> str | None:
        """Last fallback for optional Crawl4AI Markdown extraction.

        Crawl4AI is never used for robots-blocked URLs because ``extract``
        performs the policy check before entering this method.
        """
        try:
            from crawl4ai import AsyncWebCrawler

            async def collect() -> str | None:
                async with AsyncWebCrawler() as crawler:
                    result = await crawler.arun(url=url)
                    markdown = getattr(result, "markdown", None)
                    value = getattr(markdown, "fit_markdown", None) or getattr(markdown, "raw_markdown", None) or markdown
                    return str(value).strip() if value else None

            try:
                asyncio.get_running_loop()
                return None
            except RuntimeError:
                return asyncio.run(collect())
        except Exception:
            return None

    def extract_many(self, urls: list[str], *, max_workers: int | None = None, respect_robots: bool = True) -> dict[str, dict[str, str] | Exception]:
        """Fetch independent competitor URLs concurrently while preserving errors.

        When Scrapy is installed, a bounded, one-shot worker handles the
        complete batch with ``ROBOTSTXT_OBEY`` and per-domain AutoThrottle.
        Starting Scrapy in a worker process avoids a Twisted reactor inside
        this long-running HTTP server.  If the optional worker is unavailable,
        the normal per-URL path keeps the same strict robots behaviour.
        """
        unique = list(dict.fromkeys(urls))
        if not unique:
            return {}
        # Test doubles and integrations sometimes replace ``extract`` on an
        # instance. Preserve that contract instead of silently sending their
        # URLs to the independent Scrapy subprocess.
        default_extractor = getattr(self.extract, "__func__", None) is BrowserCompetitorContentClient.extract
        if respect_robots and default_extractor:
            scrapy_batch = self._scrapy_fetch_many(unique)
            if scrapy_batch is not None:
                return self._parse_scrapy_batch(scrapy_batch)
        result: dict[str, dict[str, str] | Exception] = {}
        with ThreadPoolExecutor(max_workers=min(max_workers or self.max_workers, len(unique))) as executor:
            if respect_robots:
                # Preserve the ordinary call shape for existing callers and
                # injected test clients.
                futures = {executor.submit(self.extract, url=url): url for url in unique}
            else:
                futures = {executor.submit(self.extract, url=url, respect_robots=False): url for url in unique}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    result[url] = future.result()
                except Exception as error:  # retained for the research-run log
                    result[url] = error
        return result

    def preview_extract_many(self, urls: list[str], *, max_workers: int | None = None, respect_robots: bool = True) -> dict[str, dict[str, str] | Exception]:
        """Quickly check whether public pages have usable static article text.

        This deliberately does *not* launch Scrapy, Playwright, or Crawl4AI.
        The content-production test button is an interactive diagnostic: it
        must return a trustworthy answer promptly instead of making a user
        wait for every optional rendering fallback across a large SERP.
        Formal competitor learning continues to use :meth:`extract_many` and
        its full, robots-compliant extraction chain.
        """
        unique = list(dict.fromkeys(urls))
        if not unique:
            return {}

        def extract_static(url: str) -> dict[str, str]:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise CompetitorContentProtocolError("Competitor URL is not a public HTTP(S) page.")
            if respect_robots and _robots_allows(url, timeout=self.timeout) is False:
                raise CompetitorContentProtocolError("Competitor page is blocked by robots.txt.")
            try:
                html = self._fetch_static_html(url)
            except CompetitorContentProtocolError:
                raise
            except Exception as error:
                raise CompetitorContentProtocolError(f"Competitor HTTP fetch failed: {type(error).__name__}.") from error
            title, content, extractor = self._extract_article(html, parsed.hostname)
            if len(content) < 500:
                raise CompetitorContentProtocolError("Competitor page has insufficient readable static article text.")
            return {
                "title": title or parsed.hostname,
                "content": content,
                "domain": parsed.hostname.removeprefix("www."),
                "extractor": extractor,
            }

        result: dict[str, dict[str, str] | Exception] = {}
        with ThreadPoolExecutor(max_workers=min(max_workers or self.max_workers, len(unique))) as executor:
            futures = {executor.submit(extract_static, url): url for url in unique}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    result[url] = future.result()
                except Exception as error:
                    result[url] = error
        return result

    def _scrapy_fetch_many(self, urls: list[str]) -> dict[str, dict[str, str]] | None:
        """Run the optional Scrapy worker; ``None`` means use the fallback."""
        try:
            import scrapy  # noqa: F401 - verifies the optional worker can import Scrapy
        except ImportError:
            return None
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[2])
        environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "seo_control.application.competitor_scrapy_worker"],
                input=json.dumps({"urls": urls, "timeout": self.timeout, "max_bytes": max(self.max_page_chars * 12, 2_500_000)}),
                text=True,
                capture_output=True,
                timeout=max(20, self.timeout * len(urls) + 12),
                env=environment,
                check=False,
            )
            if completed.returncode != 0:
                return None
            payload = json.loads(completed.stdout)
            rows = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                return None
            return {
                str(row["url"]): {str(key): str(value) for key, value in row.items() if key != "url"}
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("url"), str)
            }
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
            return None

    def _parse_scrapy_batch(self, batch: dict[str, dict[str, str]]) -> dict[str, dict[str, str] | Exception]:
        result: dict[str, dict[str, str] | Exception] = {}
        for url, row in batch.items():
            if row.get("status") != "ok":
                result[url] = CompetitorContentProtocolError(row.get("error") or "Scrapy could not collect this public page.")
                continue
            try:
                parsed = urlparse(row.get("final_url") or url)
                title, content, extractor = self._extract_article(row.get("html") or "", parsed.hostname or "")
                if len(content) < 500:
                    rendered = self._render_with_playwright(url)
                    if rendered:
                        title, content, extractor = self._extract_article(rendered, parsed.hostname or "")
                if len(content) < 500:
                    crawl4ai = self._extract_with_crawl4ai(url)
                    if crawl4ai:
                        content, extractor = crawl4ai[: self.max_page_chars], "crawl4ai"
                if len(content) < 500:
                    raise CompetitorContentProtocolError("Competitor page is not a usable article page.")
                result[url] = {"title": title or parsed.hostname or "Article", "content": content, "domain": (parsed.hostname or "").removeprefix("www."), "extractor": extractor}
            except Exception as error:
                result[url] = error
        return result
