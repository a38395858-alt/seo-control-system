"""Contract tests for the OpenAI-compatible keyword-review adapter.

These tests deliberately use an injected transport.  They must never load a
local provider configuration or make a real network request.
"""

from __future__ import annotations

import json

import pytest

from seo_control.application.ai_keyword_reviewer import (
    KeywordReviewProtocolError,
    OpenAICompatibleKeywordReviewer,
)


def test_reviewer_sends_openai_chat_request_and_parses_structured_review() -> None:
    captured: dict[str, object] = {}

    def transport(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "is_seo_content_fit": True,
                                "same_topic_as_seed": True,
                                "search_intent": "informational",
                                "recommended_action": "create_article",
                                "reason": "The query asks for guidance related to the seed topic.",
                                "confidence": 0.91,
                            }
                        )
                    }
                }
            ]
        }

    reviewer = OpenAICompatibleKeywordReviewer(
        api_key="test-key",
        base_url="https://ai.example.test/v1",
        model="test-model",
        transport=transport,
    )

    review = reviewer.review(seed_keyword="seo tools", keyword="best seo tools for beginners")

    assert captured["url"] == "https://ai.example.test/v1/chat/completions"
    assert captured["headers"] == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
    }
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "test-model"
    assert payload["stream"] is False
    assert isinstance(payload["messages"], list)
    assert any("seo tools" in message["content"] for message in payload["messages"])
    assert any("best seo tools for beginners" in message["content"] for message in payload["messages"])

    assert review.is_seo_content_fit is True
    assert review.same_topic_as_seed is True
    assert review.search_intent == "informational"
    assert review.recommended_action == "create_article"
    assert review.reason == "The query asks for guidance related to the seed topic."
    assert review.confidence == 0.91


def test_reviewer_rejects_invalid_model_json() -> None:
    def transport(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        return {"choices": [{"message": {"content": "This is not JSON."}}]}

    reviewer = OpenAICompatibleKeywordReviewer(
        api_key="test-key",
        base_url="https://ai.example.test",
        model="test-model",
        transport=transport,
    )

    with pytest.raises(KeywordReviewProtocolError):
        reviewer.review(seed_keyword="seo tools", keyword="seo tools free")
