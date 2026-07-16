"""Red-first acceptance tests for keyword normalization and filtering rules.

These tests define the small, pure domain API required by the keyword-mining
foundation.  They deliberately avoid databases, API clients, and frameworks.
"""

import unittest

from seo_control.domain.keywords import (
    build_keyword_dedup_key,
    normalize_keyword,
    validate_keyword,
)


class NormalizeKeywordTests(unittest.TestCase):
    def test_normalize_trims_and_collapses_chinese_and_english_whitespace(self):
        self.assertEqual(
            normalize_keyword("  Google\t关键词\n  research　tool  "),
            "google 关键词 research tool",
        )

    def test_normalize_converts_full_width_ascii_and_lowercases_english(self):
        self.assertEqual(
            normalize_keyword("Ｇｏｏｇｌｅ　ＡＤＳ　２０２６"),
            "google ads 2026",
        )

    def test_normalize_keeps_chinese_characters_while_lowercasing_latin_text(self):
        self.assertEqual(normalize_keyword("SEO关键词Tool"), "seo关键词tool")


class KeywordDedupKeyTests(unittest.TestCase):
    def test_same_normalized_keyword_in_same_market_has_same_key(self):
        first = build_keyword_dedup_key(" ＳＥＯ　Tool ", language="en", country="US")
        second = build_keyword_dedup_key("seo tool", language="en", country="us")

        self.assertEqual(first, second)
        self.assertEqual(first, "seo tool|en|us")

    def test_same_keyword_in_different_language_or_country_is_not_a_duplicate(self):
        us_english = build_keyword_dedup_key("seo tool", language="en", country="US")
        gb_english = build_keyword_dedup_key("seo tool", language="en", country="GB")
        us_chinese = build_keyword_dedup_key("seo tool", language="zh-CN", country="US")

        self.assertNotEqual(us_english, gb_english)
        self.assertNotEqual(us_english, us_chinese)


class KeywordValidationTests(unittest.TestCase):
    def test_rejects_keyword_shorter_than_minimum_length_after_normalization(self):
        result = validate_keyword("  a  ", min_length=2, negative_terms=[])

        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "too_short")

    def test_allows_keyword_at_minimum_length(self):
        result = validate_keyword(" AI ", min_length=2, negative_terms=[])

        self.assertTrue(result.is_valid)
        self.assertIsNone(result.reason)
        self.assertEqual(result.normalized_keyword, "ai")

    def test_rejects_keyword_when_a_negative_term_matches_case_insensitively(self):
        result = validate_keyword(
            "Free SEO Tool",
            min_length=2,
            negative_terms=["free", "jobs"],
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "negative_term")
        self.assertEqual(result.matched_negative_term, "free")

    def test_negative_term_matching_uses_normalized_full_width_text(self):
        result = validate_keyword(
            "Ｇｏｏｇｌｅ　Ａｄｓ　ＪＯＢＳ",
            min_length=2,
            negative_terms=["jobs"],
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "negative_term")
        self.assertEqual(result.matched_negative_term, "jobs")


if __name__ == "__main__":
    unittest.main()
