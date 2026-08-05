"""Serper.dev organic-search adapter used when browser Google is unavailable."""

from __future__ import annotations

import json
import socket
import ssl
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class SerperSearchProtocolError(RuntimeError):
    """Serper did not return a usable organic-result response."""


class SerperSearchClient:
    endpoint = "https://google.serper.dev/search"

    def __init__(self, api_key: str, *, timeout: int = 25) -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout

    def search(self, *, query: str, locale: str = "en-US", max_results: int = 10, page: int = 1, tbs: str | None = None) -> list[dict[str, Any]]:
        if not self.api_key:
            raise SerperSearchProtocolError("Serper API key is not configured.")
        language, country = (locale.split("-", 1) + ["US"])[:2] if "-" in locale else ("en", "US")
        request_payload: dict[str, Any] = {
            "q": query,
            "num": max(1, min(int(max_results), 10)),
            "page": max(1, int(page)),
            "gl": country.lower(),
            "hl": language,
        }
        if tbs:
            request_payload["tbs"] = tbs
        payload = json.dumps(request_payload).encode("utf-8")
        raw = ""
        last_error: Exception | None = None
        # Serper can close a reused TLS connection before it sends headers.
        # Build a fresh short-lived request for every safe, read-only retry.
        max_attempts = 6
        transient_http_codes = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
        for attempt in range(max_attempts):
            request = Request(
                self.endpoint,
                data=payload,
                headers={
                    "X-API-KEY": self.api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "close",
                    "User-Agent": "SEO-Control-Competitor-Research/1.0",
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:  # nosec B310 - configured Serper HTTPS endpoint
                    raw = response.read().decode("utf-8")
                last_error = None
                break
            except HTTPError as error:
                if error.code not in transient_http_codes:
                    raise SerperSearchProtocolError(f"Serper request was rejected with HTTP {error.code}.") from error
                last_error = error
            except (URLError, ssl.SSLError, TimeoutError, socket.timeout, ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as error:
                last_error = error
            except OSError as error:
                last_error = error
            if attempt + 1 < max_attempts:
                time.sleep(0.75 * (2**attempt))
        if last_error is not None:
            if isinstance(last_error, HTTPError):
                raise SerperSearchProtocolError(f"Serper request failed after {max_attempts} attempts: upstream HTTP {last_error.code}.") from last_error
            reason = last_error.reason if isinstance(last_error, URLError) else last_error
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise SerperSearchProtocolError(f"Serper request timed out after {max_attempts} attempts.") from last_error
            raise SerperSearchProtocolError(f"Serper network request failed after {max_attempts} attempts: {type(reason).__name__}.") from last_error
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
