"""Unit tests for a local, injected OpenAI-compatible keyword reviewer."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seo_control.application.ai_keyword_reviewer import KeywordReviewProtocolError, OpenAICompatibleKeywordReviewer


class AiKeywordReviewerTests(unittest.TestCase):
    def test_reviewer_sends_openai_request_and_parses_review_json(self) -> None:
        captured: dict[str, object] = {}
        def transport(url, headers, payload):
            captured.update(url=url, headers=headers, payload=payload)
            return {"choices": [{"message": {"content": json.dumps({"is_seo_content_fit": True, "same_topic_as_seed": True, "search_intent": "informational", "recommended_action": "create_article", "reason": "Relevant topic.", "confidence": 0.91})}}]}
        reviewer = OpenAICompatibleKeywordReviewer("test-key", "https://ai.example.test/v1", "test-model", transport=transport)
        review = reviewer.review(seed_keyword="seo tools", keyword="best seo tools", language="en")
        self.assertEqual("https://ai.example.test/v1/chat/completions", captured["url"])
        self.assertEqual("test-model", captured["payload"]["model"])
        self.assertFalse(captured["payload"]["stream"])
        self.assertTrue(review.is_seo_content_fit)
        self.assertTrue(review.same_topic_as_seed)
        self.assertEqual("create_article", review.recommended_action)

    def test_reviewer_rejects_invalid_model_json(self) -> None:
        reviewer = OpenAICompatibleKeywordReviewer("test-key", "https://ai.example.test", "test-model", transport=lambda *_: {"choices": [{"message": {"content": "not json"}}]})
        with self.assertRaises(KeywordReviewProtocolError):
            reviewer.review(seed_keyword="seo tools", keyword="seo tools free", language="en")
