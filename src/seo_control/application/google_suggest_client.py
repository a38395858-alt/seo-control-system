"""Small, injectable client for Google autocomplete suggestions."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class GoogleSuggestProtocolError(RuntimeError):
    """Raised when a Google Suggest response cannot be understood safely."""

    def __init__(self, message: str, *, error_code: str = "protocol_error") -> None:
        super().__init__(message)
        self.error_code = error_code


class GoogleSuggestClient:
    """Fetch Google Firefox-format autocomplete suggestions.

    ``fetch_json`` is injectable so callers and tests can control transport
    without issuing real network requests.
    """

    _ENDPOINT = "https://suggestqueries.google.com/complete/search"

    def __init__(self, fetch_json: Callable[[str], object] | None = None) -> None:
        self._fetch_json = fetch_json or self._default_fetch_json

    def fetch(self, query: str, *, hl: str, gl: str) -> list[str]:
        url = self._build_url(query=query, hl=hl, gl=gl)

        try:
            response = self._fetch_json(url)
        except GoogleSuggestProtocolError:
            raise
        except TimeoutError as exc:
            raise GoogleSuggestProtocolError("Google Suggest connection timed out.", error_code="network_timeout") from exc
        except Exception as exc:
            raise GoogleSuggestProtocolError("Google Suggest request failed.", error_code="network_error") from exc

        try:
            return self._parse_response(response)
        except GoogleSuggestProtocolError:
            raise

    def suggest(self, query: str, *, hl: str, gl: str) -> list[str]:
        """Alias for :meth:`fetch`, used by keyword-expansion services."""
        return self.fetch(query, hl=hl, gl=gl)

    @classmethod
    def _build_url(cls, *, query: str, hl: str, gl: str) -> str:
        parameters = urlencode(
            {
                "client": "firefox",
                "q": query,
                "hl": hl,
                "gl": gl,
            }
        )
        return f"{cls._ENDPOINT}?{parameters}"

    @staticmethod
    def _parse_response(response: object) -> list[str]:
        if not isinstance(response, list) or len(response) < 2:
            raise GoogleSuggestProtocolError("Unexpected Google Suggest response structure")

        suggestions = response[1]
        if not isinstance(suggestions, list):
            raise GoogleSuggestProtocolError("Google Suggest suggestions must be a list")

        cleaned: list[str] = []
        seen: set[str] = set()
        for suggestion in suggestions:
            if not isinstance(suggestion, str):
                raise GoogleSuggestProtocolError("Google Suggest suggestion must be a string")

            normalized = suggestion.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                cleaned.append(normalized)

        return cleaned

    @staticmethod
    def _default_fetch_json(url: str) -> object:
        try:
            request = Request(
                url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "User-Agent": "Mozilla/5.0 (compatible; SEOControlKeywordResearch/1.0)",
                },
            )
            with urlopen(request, timeout=10) as response:  # nosec B310 - fixed HTTPS endpoint
                payload: Any = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            code = "http_rate_limited" if exc.code == 429 else "http_forbidden" if exc.code == 403 else "http_error"
            raise GoogleSuggestProtocolError(f"Google Suggest returned HTTP {exc.code}.", error_code=code) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise GoogleSuggestProtocolError("Google Suggest connection timed out.", error_code="network_timeout") from exc
        except URLError as exc:
            raise GoogleSuggestProtocolError("Google Suggest network connection failed.", error_code="network_error") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleSuggestProtocolError("Google Suggest returned an unreadable response.", error_code="decode_error") from exc
        return payload
