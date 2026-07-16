"""Red tests for bounded breadth-first Google Suggest keyword expansion.

The expansion service is intentionally independent from HTTP.  Its client only
needs to implement ``suggest(query, *, hl, gl) -> list[str]``; the Google
protocol adapter will live behind that seam.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


from seo_control.application.keyword_expansion_service import KeywordExpansionService


class FakeSuggestClient:
    """Deterministic protocol-client double that records every request."""

    def __init__(self, responses: dict[str, list[str]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, str]] = []

    def suggest(self, query: str, *, hl: str, gl: str) -> list[str]:
        self.calls.append((query, hl, gl))
        return self.responses.get(query, [])


class KeywordExpansionServiceTests(unittest.TestCase):
    """Behaviour contract for safe recursive keyword expansion."""

    def test_expands_breadth_first_and_deduplicates_across_seed_and_suggestions(self) -> None:
        client = FakeSuggestClient(
            {
                "seed one": ["alpha", "shared"],
                "seed two": ["beta", "shared"],
                "alpha": ["alpha child"],
                "shared": ["shared child"],
                "beta": ["beta child"],
            }
        )
        service = KeywordExpansionService(client)

        result = service.expand(
            ["seed one", "seed two", "seed one"],
            hl="en",
            gl="US",
            max_keywords=20,
            max_requests=20,
            max_depth=2,
        )

        self.assertEqual(
            result.keywords,
            [
                "seed one",
                "seed two",
                "alpha",
                "shared",
                "beta",
                "alpha child",
                "shared child",
                "beta child",
            ],
        )
        self.assertEqual(
            client.calls,
            [
                ("seed one", "en", "US"),
                ("seed two", "en", "US"),
                ("alpha", "en", "US"),
                ("shared", "en", "US"),
                ("beta", "en", "US"),
            ],
        )
        self.assertEqual(result.requests_made, 5)
        self.assertEqual(result.stop_reason, "max_depth")

    def test_stops_when_an_entire_depth_produces_no_new_keywords(self) -> None:
        client = FakeSuggestClient(
            {
                "seed": ["seed", "known"],
                "known": ["seed", "known"],
            }
        )
        service = KeywordExpansionService(client)

        result = service.expand(
            ["seed"],
            hl="zh-CN",
            gl="CN",
            max_keywords=100,
            max_requests=100,
            max_depth=10,
        )

        self.assertEqual(result.keywords, ["seed", "known"])
        self.assertEqual(result.requests_made, 2)
        self.assertEqual(result.stop_reason, "no_new_keywords")

    def test_stops_immediately_when_maximum_keyword_count_is_reached(self) -> None:
        client = FakeSuggestClient({"seed": ["one", "two", "three"]})
        service = KeywordExpansionService(client)

        result = service.expand(
            ["seed"],
            hl="en",
            gl="US",
            max_keywords=3,
            max_requests=10,
            max_depth=10,
        )

        self.assertEqual(result.keywords, ["seed", "one", "two"])
        self.assertEqual(result.requests_made, 1)
        self.assertEqual(result.stop_reason, "max_keywords")

    def test_stops_when_the_request_budget_is_exhausted(self) -> None:
        client = FakeSuggestClient(
            {"seed": ["one", "two"], "one": ["one child"], "two": ["two child"]}
        )
        service = KeywordExpansionService(client)

        result = service.expand(
            ["seed"],
            hl="en",
            gl="US",
            max_keywords=20,
            max_requests=2,
            max_depth=10,
        )

        self.assertEqual(result.keywords, ["seed", "one", "two", "one child"])
        self.assertEqual(result.requests_made, 2)
        self.assertEqual(result.stop_reason, "max_requests")
        self.assertEqual(client.calls, [("seed", "en", "US"), ("one", "en", "US")])

    def test_stops_after_the_configured_depth_without_requesting_deeper_keywords(self) -> None:
        client = FakeSuggestClient(
            {"seed": ["one"], "one": ["two"], "two": ["three"]}
        )
        service = KeywordExpansionService(client)

        result = service.expand(
            ["seed"],
            hl="en",
            gl="US",
            max_keywords=20,
            max_requests=20,
            max_depth=1,
        )

        self.assertEqual(result.keywords, ["seed", "one", "two"])
        self.assertEqual(result.requests_made, 2)
        self.assertEqual(result.stop_reason, "max_depth")
        self.assertEqual(client.calls, [("seed", "en", "US"), ("one", "en", "US")])


if __name__ == "__main__":
    unittest.main()
