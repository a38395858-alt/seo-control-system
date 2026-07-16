"""Pure keyword normalization, deduplication, and validation rules.

The functions in this module deliberately have no database or API dependency so
that all keyword sources apply the exact same rules before persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata


@dataclass(frozen=True)
class KeywordValidationResult:
    """The outcome of applying the basic keyword admission rules."""

    is_valid: bool
    reason: str | None
    normalized_keyword: str
    matched_negative_term: str | None = None


def normalize_keyword(keyword: str) -> str:
    """Return a canonical keyword suitable for comparison and display.

    Unicode NFKC converts full-width Latin characters, digits, and spaces to
    their half-width counterparts.  Collapsing whitespace avoids treating
    otherwise identical phrases as separate terms; ``casefold`` handles Latin
    case normalization more robustly than ``lower``.
    """

    if not isinstance(keyword, str):
        raise TypeError("keyword must be a string")

    normalized = unicodedata.normalize("NFKC", keyword)
    return " ".join(normalized.split()).casefold()


def build_keyword_dedup_key(keyword: str, *, language: str, country: str) -> str:
    """Build the unique comparison key within a Google Ads target market."""

    normalized_keyword = normalize_keyword(keyword)
    normalized_language = _normalize_market_value(language, "language")
    normalized_country = _normalize_market_value(country, "country")
    return f"{normalized_keyword}|{normalized_language}|{normalized_country}"


def validate_keyword(
    keyword: str,
    *,
    min_length: int,
    negative_terms: list[str],
) -> KeywordValidationResult:
    """Check a normalized keyword against length and negative-term rules."""

    if min_length < 0:
        raise ValueError("min_length must be zero or greater")

    normalized_keyword = normalize_keyword(keyword)
    if len(normalized_keyword) < min_length:
        return KeywordValidationResult(
            is_valid=False,
            reason="too_short",
            normalized_keyword=normalized_keyword,
        )

    for negative_term in negative_terms:
        normalized_negative_term = normalize_keyword(negative_term)
        if normalized_negative_term and normalized_negative_term in normalized_keyword:
            return KeywordValidationResult(
                is_valid=False,
                reason="negative_term",
                normalized_keyword=normalized_keyword,
                matched_negative_term=normalized_negative_term,
            )

    return KeywordValidationResult(
        is_valid=True,
        reason=None,
        normalized_keyword=normalized_keyword,
    )


def _normalize_market_value(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")

    normalized = normalize_keyword(value)
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized
