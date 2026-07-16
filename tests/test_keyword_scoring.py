"""Red-first unit tests for SEO keyword difficulty and opportunity scoring."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.domain.keyword_scoring import (  # noqa: E402
    KeywordScoringInput,
    calculate_keyword_score,
)


class KeywordScoringTests(unittest.TestCase):
    def test_calculates_a_bounded_difficulty_and_opportunity_score(self) -> None:
        score = calculate_keyword_score(
            KeywordScoringInput(
                monthly_search_volume=1_000,
                average_domain_authority=40,
                average_referring_domains=100,
                exact_title_match_rate=0.3,
                authority_site_ratio=0.2,
                intent_competition=3,
                relevance_score=0.9,
                business_value_score=0.8,
            )
        )

        self.assertEqual(43, score.keyword_difficulty)
        self.assertEqual("medium", score.difficulty_level)
        self.assertEqual(20, score.opportunity_score)

    def test_rejects_scores_outside_their_allowed_ranges(self) -> None:
        with self.assertRaises(ValueError):
            calculate_keyword_score(
                KeywordScoringInput(
                    monthly_search_volume=-1,
                    average_domain_authority=101,
                    average_referring_domains=0,
                    exact_title_match_rate=0,
                    authority_site_ratio=0,
                    intent_competition=1,
                    relevance_score=1,
                    business_value_score=1,
                )
            )
