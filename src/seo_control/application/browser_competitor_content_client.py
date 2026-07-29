"""Browser-backed Google result and public-page extraction for content research."""

from __future__ import annotations

import base64
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.robotparser import RobotFileParser
from urllib.request import Request, urlopen

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
    """Read up to two Google result pages then extract accessible public pages.

    Google is only queried in a visible local Chrome profile.  Competitor
    pages are then fetched over HTTP concurrently: this is materially faster
    than opening five Chrome tabs and avoids rendering scripts that are not
    part of the article evidence.
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
        request = Request(url, headers={"User-Agent": "SEOContentResearchBot/1.0 (+local content research)"})
        try:
            with urlopen(request, timeout=self.timeout) as response:  # nosec B310 - user-triggered public competitor URL
                content_type = response.headers.get_content_type()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise CompetitorContentProtocolError("Competitor URL is not an HTML content page.")
                charset = response.headers.get_content_charset() or "utf-8"
                # Modern editorial themes can put a megabyte of mega-menu
                # markup ahead of the article (Lumens is one example). Keep
                # the stored evidence capped below, but read enough HTML to
                # actually reach a late-rendered guide instead of falsely
                # classifying it as an empty page.
                html = response.read(max(self.max_page_chars * 12, 2_500_000)).decode(charset, errors="replace")
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
            loose = _LooseArticleTextParser(); loose.feed(html)
            loose_content = "\n\n".join(loose.blocks).strip()[: self.max_page_chars]
            if len(loose_content) > len(content):
                parser, content = loose, loose_content
        if len(content) < 500 or len(parser.blocks) < 3:
            raise CompetitorContentProtocolError("Competitor page is not a usable article page.")
        return {"title": " ".join(parser.title.split()) or parsed.hostname, "content": content, "domain": parsed.hostname.removeprefix("www.")}

    def extract_many(self, urls: list[str], *, max_workers: int | None = None, respect_robots: bool = True) -> dict[str, dict[str, str] | Exception]:
        """Fetch independent competitor URLs concurrently while preserving errors."""
        unique = list(dict.fromkeys(urls))
        if not unique:
            return {}
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
