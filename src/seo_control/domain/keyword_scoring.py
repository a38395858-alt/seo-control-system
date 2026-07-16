"""Deterministic SEO keyword difficulty and opportunity scoring."""

from __future__ import annotations

from dataclasses import dataclass
from math import log10


@dataclass(frozen=True)
class KeywordScoringInput:
    monthly_search_volume: int
    average_domain_authority: float
    average_referring_domains: float
    exact_title_match_rate: float
    authority_site_ratio: float
    intent_competition: int
    relevance_score: float
    business_value_score: float


@dataclass(frozen=True)
class KeywordScore:
    keyword_difficulty: int
    difficulty_level: str
    opportunity_score: int


def calculate_keyword_score(values: KeywordScoringInput) -> KeywordScore:
    """Return a reproducible 0-100 KD and opportunity score.

    KD weights domain strength, referring domains, exact title matches,
    authority-site presence and search-intent competition. Opportunity rewards
    relevant, valuable keywords with search demand that are still attainable.
    """

    _validate(values)
    referring_domain_score = min(log10(values.average_referring_domains + 1) / log10(1001) * 100, 100)
    intent_score = (values.intent_competition - 1) / 4 * 100
    difficulty = round(
        values.average_domain_authority * 0.30
        + referring_domain_score * 0.25
        + values.exact_title_match_rate * 100 * 0.20
        + values.authority_site_ratio * 100 * 0.15
        + intent_score * 0.10
    )
    difficulty = max(0, min(100, difficulty))
    volume_factor = min(log10(values.monthly_search_volume + 1) / 6, 1)
    opportunity = int(
        100
        * volume_factor
        * (1 - difficulty / 100)
        * values.relevance_score
        * values.business_value_score
    )
    return KeywordScore(difficulty, _difficulty_level(difficulty), max(0, min(100, opportunity)))


def _difficulty_level(value: int) -> str:
    if value < 30:
        return "low"
    if value < 50:
        return "medium"
    if value < 70:
        return "high"
    return "very_high"


def _validate(values: KeywordScoringInput) -> None:
    if values.monthly_search_volume < 0:
        raise ValueError("monthly_search_volume must be non-negative")
    if not 0 <= values.average_domain_authority <= 100:
        raise ValueError("average_domain_authority must be between 0 and 100")
    if values.average_referring_domains < 0:
        raise ValueError("average_referring_domains must be non-negative")
    for name, value in (
        ("exact_title_match_rate", values.exact_title_match_rate),
        ("authority_site_ratio", values.authority_site_ratio),
        ("relevance_score", values.relevance_score),
        ("business_value_score", values.business_value_score),
    ):
        if not 0 <= value <= 1:
            raise ValueError(f"{name} must be between 0 and 1")
    if not 1 <= values.intent_competition <= 5:
        raise ValueError("intent_competition must be between 1 and 5")
