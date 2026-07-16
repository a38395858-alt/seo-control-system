"""Bounded breadth-first expansion of Google suggestion keywords."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from seo_control.application.google_suggest_client import GoogleSuggestProtocolError
from seo_control.domain.keywords import normalize_keyword


class SuggestClient(Protocol):
    def suggest(self, query: str, *, hl: str, gl: str) -> list[str]: ...


@dataclass(frozen=True)
class KeywordExpansionResult:
    keywords: list[str]
    requests_made: int
    stop_reason: str
    debug_logs: list[dict[str, str]] = field(default_factory=list)


class KeywordExpansionService:
    """Safely expand suggestions until exhausted or a caller limit is reached."""

    def __init__(self, client: SuggestClient) -> None:
        self._client = client

    def expand(
        self,
        seed_keywords: Sequence[str],
        *,
        hl: str,
        gl: str,
        max_keywords: int = 10_000,
        max_requests: int = 10_000,
        max_depth: int = 10,
    ) -> KeywordExpansionResult:
        self._validate_limits(max_keywords, max_requests, max_depth)
        language = self._market_value(hl, "hl")
        country = self._market_value(gl, "gl")
        keywords: list[str] = []
        debug_logs: list[dict[str, str]] = []
        seen: set[str] = set()
        queue: deque[tuple[str, int]] = deque()

        for seed in seed_keywords:
            keyword = self._keyword(seed)
            if not keyword or keyword in seen:
                continue
            if len(keywords) >= max_keywords:
                return KeywordExpansionResult(keywords, 0, "max_keywords", debug_logs)
            seen.add(keyword)
            keywords.append(keyword)
            queue.append((keyword, 0))
        if not queue:
            return KeywordExpansionResult(keywords, 0, "no_new_keywords", debug_logs)
        if max_depth == 0:
            return KeywordExpansionResult(keywords, 0, "max_depth", debug_logs)

        # Depth one intentionally includes querying the first suggestions, so
        # a user receives a useful second-hop expansion without unbounded work.
        maximum_query_depth = max(1, max_depth - 1)
        requests_made = 0
        while queue:
            query, depth = queue[0]
            if depth > maximum_query_depth:
                return KeywordExpansionResult(keywords, requests_made, "max_depth", debug_logs)
            if requests_made >= max_requests:
                return KeywordExpansionResult(keywords, requests_made, "max_requests", debug_logs)
            queue.popleft()
            requests_made += 1
            try:
                suggestions = self._client.suggest(query, hl=language, gl=country)
            except GoogleSuggestProtocolError as error:
                debug_logs.append(
                    {
                        "level": "error",
                        "event": "google_suggest_failed",
                        "code": error.error_code,
                        "message": str(error),
                    }
                )
                continue
            for suggestion in suggestions:
                keyword = self._keyword(suggestion)
                if not keyword or keyword in seen:
                    continue
                seen.add(keyword)
                keywords.append(keyword)
                queue.append((keyword, depth + 1))
                if len(keywords) >= max_keywords:
                    return KeywordExpansionResult(keywords, requests_made, "max_keywords", debug_logs)
        stop_reason = "completed_with_errors" if debug_logs else "no_new_keywords"
        return KeywordExpansionResult(keywords, requests_made, stop_reason, debug_logs)

    @staticmethod
    def _keyword(value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("keywords returned by the client must be strings")
        return normalize_keyword(value)

    @staticmethod
    def _market_value(value: str, field_name: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field_name} must be a string")
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError(f"{field_name} must not be empty")
        return cleaned

    @staticmethod
    def _validate_limits(max_keywords: int, max_requests: int, max_depth: int) -> None:
        for name, value in (("max_keywords", max_keywords), ("max_requests", max_requests), ("max_depth", max_depth)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
