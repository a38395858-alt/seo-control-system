"""Browser-backed Google result and public-page extraction for content research."""

from __future__ import annotations

from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from urllib.request import Request, urlopen

from seo_control.application.browser_serp_title_client import (
    BrowserSerpTitleClient,
    GoogleSerpProtocolError,
    _GoogleResultParser,
)


class CompetitorContentProtocolError(GoogleSerpProtocolError):
    """A public competitor page cannot be safely used as a source."""


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


class BrowserCompetitorContentClient:
    """Read up to two Google result pages then extract accessible public pages.

    Google is only queried in a visible local Chrome profile.  Competitor
    pages are then fetched over HTTP concurrently: this is materially faster
    than opening five Chrome tabs and avoids rendering scripts that are not
    part of the article evidence.
    """

    def __init__(self, browser: BrowserSerpTitleClient | None = None, *, max_page_chars: int = 60_000, timeout: int = 15, max_workers: int = 5) -> None:
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

    def extract(self, *, url: str) -> dict[str, str]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise CompetitorContentProtocolError("Competitor URL is not a public HTTP(S) page.")
        robots = RobotFileParser(); robots.set_url(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        try:
            robots.read()
            if not robots.can_fetch("SEOContentResearchBot", url):
                raise CompetitorContentProtocolError("Competitor page is blocked by robots.txt.")
        except CompetitorContentProtocolError:
            raise
        except Exception:
            # A missing or temporarily unavailable robots file is recorded by
            # the caller but does not authorise bypassing an explicit block.
            pass
        request = Request(url, headers={"User-Agent": "SEOContentResearchBot/1.0 (+local content research)"})
        try:
            with urlopen(request, timeout=self.timeout) as response:  # nosec B310 - user-triggered public competitor URL
                content_type = response.headers.get_content_type()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise CompetitorContentProtocolError("Competitor URL is not an HTML content page.")
                charset = response.headers.get_content_charset() or "utf-8"
                # Modern commerce themes may put several hundred kilobytes of
                # CSS/navigation before the actual article. Keep the stored
                # article cap unchanged, but read enough HTML to reach it.
                html = response.read(self.max_page_chars * 12).decode(charset, errors="replace")
        except CompetitorContentProtocolError:
            raise
        except Exception as error:
            raise CompetitorContentProtocolError(f"Competitor HTTP fetch failed: {type(error).__name__}.") from error
        parser = _ArticleTextParser(); parser.feed(html)
        content = "\n\n".join(parser.blocks).strip()[: self.max_page_chars]
        if len(content) < 500 or len(parser.blocks) < 3:
            fallback = _FallbackArticleTextParser(); fallback.feed(html)
            fallback_content = "\n\n".join(fallback.blocks).strip()[: self.max_page_chars]
            if len(fallback_content) > len(content):
                parser, content = fallback, fallback_content
        if len(content) < 500 or len(parser.blocks) < 3:
            raise CompetitorContentProtocolError("Competitor page is not a usable article page.")
        return {"title": " ".join(parser.title.split()) or parsed.hostname, "content": content, "domain": parsed.hostname.removeprefix("www.")}

    def extract_many(self, urls: list[str], *, max_workers: int | None = None) -> dict[str, dict[str, str] | Exception]:
        """Fetch independent competitor URLs concurrently while preserving errors."""
        unique = list(dict.fromkeys(urls))
        if not unique:
            return {}
        result: dict[str, dict[str, str] | Exception] = {}
        with ThreadPoolExecutor(max_workers=min(max_workers or self.max_workers, len(unique))) as executor:
            futures = {executor.submit(self.extract, url=url): url for url in unique}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    result[url] = future.result()
                except Exception as error:  # retained for the research-run log
                    result[url] = error
        return result
