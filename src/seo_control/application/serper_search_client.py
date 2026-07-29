"""Serper.dev organic-search adapter used when browser Google is unavailable."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class SerperSearchProtocolError(RuntimeError):
    """Serper did not return a usable organic-result response."""


class SerperSearchClient:
    endpoint = "https://google.serper.dev/search"

    def __init__(self, api_key: str, *, timeout: int = 25) -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout

    def search(self, *, query: str, locale: str = "en-US", max_results: int = 10) -> list[dict[str, Any]]:
        if not self.api_key:
            raise SerperSearchProtocolError("Serper API key is not configured.")
        language, country = (locale.split("-", 1) + ["US"])[:2] if "-" in locale else ("en", "US")
        payload = json.dumps({"q": query, "num": max_results, "gl": country.lower(), "hl": language}).encode("utf-8")
        request = Request(self.endpoint, data=payload, headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:  # nosec B310 - configured Serper HTTPS endpoint
                raw = response.read().decode("utf-8")
        except Exception as error:
            raise SerperSearchProtocolError(f"Serper request failed: {type(error).__name__}.") from error
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise SerperSearchProtocolError("Serper returned invalid JSON.") from error
        organic = document.get("organic") if isinstance(document, dict) else None
        if not isinstance(organic, list):
            raise SerperSearchProtocolError("Serper returned no organic search results.")
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in organic:
            if not isinstance(item, dict):
                continue
            url, title = item.get("link"), item.get("title")
            parsed = urlparse(url) if isinstance(url, str) else None
            if not isinstance(title, str) or not parsed or parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            normalized = url.split("#", 1)[0].rstrip("/").casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            results.append({"rank": int(item.get("position") or len(results) + 1), "title": " ".join(title.split()), "url": url, "domain": parsed.hostname.removeprefix("www.")})
            if len(results) >= max_results:
                break
        if not results:
            raise SerperSearchProtocolError("Serper returned no readable organic result URLs.")
        return results
