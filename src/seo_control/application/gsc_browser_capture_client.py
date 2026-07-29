"""Visible-browser capture for a user-authenticated Search Console session.

The local Chrome profile is deliberately isolated from the user's everyday
browser.  A user signs in on the visible Google page themselves; this client
never reads passwords, cookies, or Chrome's credential store.  It only reads
the currently rendered report table through Chrome DevTools after the user
has navigated to Search Console Performance.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from seo_control.application.browser_serp_title_client import BrowserSerpTitleClient, GoogleSerpProtocolError


class GscBrowserCaptureError(RuntimeError):
    """The user-facing browser report cannot be read yet."""


class GscBrowserCaptureClient:
    # Common closed-class and product words seen in the non-English queries of
    # US properties.  This complements the script check below for languages
    # that use the same ASCII alphabet as English.
    _NON_ENGLISH_QUERY_TOKENS = frozenset({
        "aussen", "beleuchtung", "con", "das", "de", "del", "der", "des", "di", "die", "eclairage", "el", "en", "escalera", "et", "fuer", "il", "la", "las", "le", "les", "luci", "luces", "lumiere", "mit", "para", "per", "por", "pour", "scala", "treppen", "und", "una", "uno", "y",
    })

    def __init__(self) -> None:
        self._chrome = BrowserSerpTitleClient(timeout=45)

    def open_console(self, *, property_url: str = "") -> dict[str, str]:
        self._chrome._ensure_chrome()  # local Chrome is intentionally kept open for user login
        # Do not preserve the filters from an already-open GSC report. This
        # button is deliberately deterministic: every click opens the same
        # US, last-seven-days query report before the rank <= 40 selection.
        destination = "https://search.google.com/search-console"
        if property_url.startswith(("http://", "https://")):
            destination = "https://search.google.com/search-console/performance/search-analytics?" + urlencode({"resource_id": property_url, "breakdown": "query", "metrics": "CLICKS,IMPRESSIONS,POSITION", "last_7_days": "true", "country": "usa"})
        self._navigate(destination)
        return {"status": "browser_open", "message": "Chrome 已打开该网站的 Search Console 效果报告，默认查询最近 7 天、国家/地区为美国，并启用查询、点击、展示和排名。请自行登录（如需要）并保持表格可见，然后返回此处开始采集。"}

    def capture_visible_rows(self, *, fallback_page_url: str) -> list[dict[str, Any]]:
        document = self._report_document()
        items = [item for item in self._parse_visible_rows(document, fallback_page_url=fallback_page_url) if self._is_english_query(str(item["query"]))]
        if not items:
            raise GscBrowserCaptureError("No English-only Search Console query rows were found. In the Chrome window open Performance, wait for the query table to load, then retry.")
        return items[:500]

    def capture_ranked_query_pages(self, *, fallback_page_url: str, max_position: float = 40, max_queries: int = 100) -> dict[str, Any]:
        """Map visible Performance queries to their real GSC page URLs.

        Search Console's downloadable query and page tables are separate
        aggregates.  A URL may only be assigned to a query after GSC has
        applied that query as a filter.  This browser-only flow does exactly
        that: read the query table, keep eligible rankings, navigate to the
        same report with ``query=!<query>`` and ``breakdown=page``, then read
        the resulting page table.  It does not read cookies or call GSC APIs.
        """
        report = self._report_document()
        current_url = str(report.get("url") or "")
        if "search.google.com/search-console" not in current_url:
            raise GscBrowserCaptureError("Open Google Search Console > Performance first, then retry the ranked-page capture.")
        query_url = self._performance_url(current_url, breakdown="query", last_7_days=True)
        self._navigate(query_url)
        query_rows = self._parse_visible_rows(self._wait_for_report(query_url, expect_pages=False), fallback_page_url=fallback_page_url)
        if any(str(row["query"]).startswith(("http://", "https://")) for row in query_rows):
            raise GscBrowserCaptureError("Search Console did not switch to the Queries table. Wait for the table to load and retry.")
        ranked_rows = [row for row in query_rows if 0 < float(row["position"]) <= max_position]
        non_english_skipped = sum(1 for row in ranked_rows if not self._is_english_query(str(row["query"])))
        eligible = [row for row in ranked_rows if self._is_english_query(str(row["query"]))][:max_queries]
        if not eligible:
            raise GscBrowserCaptureError(f"No English-only query rows with an average position of {max_position:g} or better were found.")

        mapped: list[dict[str, Any]] = []
        skipped = 0
        try:
            for query_row in eligible:
                page_url = self._performance_url(current_url, breakdown="page", query=str(query_row["query"]), last_7_days=True)
                self._navigate(page_url)
                page_rows = self._parse_visible_rows(self._wait_for_report(page_url, expect_pages=True), fallback_page_url="")
                matched_pages = [row for row in page_rows if str(row["query"]).startswith(("http://", "https://"))]
                if not matched_pages:
                    skipped += 1
                    continue
                for page in matched_pages:
                    mapped.append({
                        "query": query_row["query"],
                        "page_url": page["query"],
                        "clicks": page["clicks"],
                        "impressions": page["impressions"],
                        "ctr": page["ctr"],
                        "position": page["position"],
                    })
        finally:
            self._navigate(query_url)
        if not mapped:
            raise GscBrowserCaptureError("GSC returned no page URL for the eligible visible queries. Keep the Performance report open and retry after its page table finishes loading.")
        return {"rows": mapped, "queries_checked": len(eligible), "queries_without_page": skipped, "queries_non_english": non_english_skipped, "max_position": max_position}

    def _report_document(self) -> dict[str, Any]:
        self._chrome._ensure_chrome()
        expression = """JSON.stringify({url:location.href,rows:Array.from(document.querySelectorAll('table tr,[role=row]')).map(row=>({cells:Array.from(row.querySelectorAll('th,td,[role=cell],[role=gridcell]')).map(cell=>({text:(cell.innerText||'').trim(),href:(cell.querySelector('a')||{}).href||''})).filter(cell=>cell.text)})).filter(row=>row.cells.length>=2)})"""
        return self._evaluate(expression)

    def _wait_for_report(self, expected_url: str, *, expect_pages: bool, timeout: float = 10) -> dict[str, Any]:
        """Read as soon as GSC renders the requested table, not after a fixed sleep."""
        expected = urlsplit(expected_url)
        deadline = time.monotonic() + timeout
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            latest = self._report_document()
            current = urlsplit(str(latest.get("url") or ""))
            if current.path == expected.path and current.query == expected.query:
                rows = self._parse_visible_rows(latest, fallback_page_url="")
                if rows and all(str(row["query"]).startswith(("http://", "https://")) for row in rows) == expect_pages:
                    return latest
            time.sleep(0.2)
        target = "page URL" if expect_pages else "query"
        raise GscBrowserCaptureError(f"Search Console did not finish loading the {target} table within {timeout:g} seconds.")

    def _parse_visible_rows(self, document: dict[str, Any], *, fallback_page_url: str) -> list[dict[str, Any]]:
        rows = document.get("rows") if isinstance(document, dict) else []
        items: list[dict[str, Any]] = []
        if isinstance(rows, list):
            for row in rows:
                cells = row.get("cells") if isinstance(row, dict) else []
                if not isinstance(cells, list) or len(cells) < 2:
                    continue
                texts = [str(cell.get("text") or "").strip() for cell in cells if isinstance(cell, dict)]
                if not texts or texts[0].casefold() in {"query", "queries", "page", "pages", "clicks", "查询数", "网页"}:
                    continue
                metrics = [self._number(value) for value in texts[1:]]
                if not any(value is not None for value in metrics):
                    continue
                link = next((str(cell.get("href")) for cell in cells if isinstance(cell, dict) and str(cell.get("href") or "").startswith("http")), fallback_page_url)
                items.append({"query": texts[0], "page_url": link, "clicks": metrics[0] or 0, "impressions": metrics[1] or 0, "ctr": (metrics[2] or 0) / 100 if len(metrics) > 2 and "%" in texts[3] else 0, "position": metrics[-1] or 0})
        return items

    @staticmethod
    def _performance_url(current_url: str, *, breakdown: str, query: str | None = None, last_7_days: bool = True) -> str:
        parts = urlsplit(current_url)
        values = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in {"breakdown", "query", "last_24_hours", "last_7_days", "num_of_days", "country"}]
        values.append(("breakdown", breakdown))
        if last_7_days:
            values.append(("last_7_days", "true"))
        # Search Console's report URL uses this ISO-like lower-case country
        # code for its visible Country/Region filter.
        values.append(("country", "usa"))
        if query:
            # This is the URL GSC itself produces after a user clicks a query row.
            values.append(("query", f"!{query}"))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(values), parts.fragment))

    def _navigate(self, url: str) -> None:
        try:
            import websocket
            connection = websocket.create_connection(self._chrome._page_websocket_url(self._chrome._port or 0), timeout=20)
            try:
                self._chrome._cdp(connection, 1, "Page.enable")
                self._chrome._cdp(connection, 2, "Page.navigate", {"url": url})
            finally:
                connection.close()
        except Exception as error:
            raise GscBrowserCaptureError(f"Unable to open the local Chrome window: {error}") from error

    def _evaluate(self, expression: str) -> dict[str, Any]:
        try:
            import websocket
            connection = websocket.create_connection(self._chrome._page_websocket_url(self._chrome._port or 0), timeout=20)
            try:
                response = self._chrome._cdp(connection, 1, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
                value = response.get("result", {}).get("result", {}).get("value")
                if not isinstance(value, str):
                    raise GscBrowserCaptureError("Chrome did not return the visible Search Console report.")
                return json.loads(value)
            finally:
                connection.close()
        except (GoogleSerpProtocolError, OSError, ValueError, json.JSONDecodeError) as error:
            raise GscBrowserCaptureError(str(error)) from error

    @staticmethod
    def _number(value: str) -> float | None:
        cleaned = value.replace(",", "").replace("%", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None

    @staticmethod
    def _is_english_query(value: str) -> bool:
        """Keep English-script queries and reject mixed or other-script text.

        Search Console does not provide a query-language filter. The first
        guard rejects every non-ASCII script and accent. A conservative token
        deny-list then catches common Spanish, French, German and Italian
        words that share ASCII with English. This keeps language-mixed queries
        out without accepting browser credentials or calling another API.
        """
        letters = [character for character in value if unicodedata.category(character).startswith("L")]
        if not letters or not all("a" <= character.casefold() <= "z" for character in letters):
            return False
        tokens = re.findall(r"[a-z]+", value.casefold())
        return not any(token in GscBrowserCaptureClient._NON_ENGLISH_QUERY_TOKENS for token in tokens)
