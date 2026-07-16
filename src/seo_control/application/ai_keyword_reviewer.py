"""OpenAI-compatible SEO suitability reviewer for expanded keywords."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.request import Request, urlopen


class KeywordReviewProtocolError(RuntimeError):
    """Raised when an AI review response is unavailable or invalid."""


@dataclass(frozen=True)
class KeywordReview:
    is_seo_content_fit: bool
    same_topic_as_seed: bool
    search_intent: str
    recommended_action: str
    reason: str
    confidence: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


Transport = Callable[[str, Mapping[str, str], Mapping[str, object]], object]


class OpenAICompatibleKeywordReviewer:
    """Review one keyword through an OpenAI-compatible chat endpoint."""

    def __init__(self, api_key: str, base_url: str, model: str, *, transport: Transport | None = None) -> None:
        if not api_key.strip() or not base_url.strip() or not model.strip():
            raise ValueError("api_key, base_url and model are required")
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._model = model.strip()
        self._transport = transport or self._default_transport

    def review(self, *, seed_keyword: str, keyword: str, language: str) -> KeywordReview:
        payload: dict[str, object] = {
            "model": self._model,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "You are an SEO keyword reviewer. Return JSON only with: is_seo_content_fit, same_topic_as_seed, search_intent, recommended_action, reason, confidence.",
                },
                {
                    "role": "user",
                    "content": json.dumps({"seed_keyword": seed_keyword, "keyword": keyword, "language": language}, ensure_ascii=False),
                },
            ],
        }
        try:
            response = self._transport(
                f"{self._base_url}/chat/completions",
                {"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"},
                payload,
            )
            return self._parse(response)
        except KeywordReviewProtocolError:
            raise
        except Exception as error:
            raise KeywordReviewProtocolError("AI keyword review request failed.") from error

    @staticmethod
    def _default_transport(url: str, headers: Mapping[str, str], payload: Mapping[str, object]) -> object:
        request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=dict(headers), method="POST")
        with urlopen(request, timeout=30) as response:  # nosec B310 - user-configured server endpoint
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _parse(response: object) -> KeywordReview:
        try:
            content = response["choices"][0]["message"]["content"]  # type: ignore[index]
            data = json.loads(content) if isinstance(content, str) else content
            if not isinstance(data, dict):
                raise TypeError("review content is not an object")
            result = KeywordReview(
                is_seo_content_fit=OpenAICompatibleKeywordReviewer._boolean(data, "is_seo_content_fit"),
                same_topic_as_seed=OpenAICompatibleKeywordReviewer._boolean(data, "same_topic_as_seed"),
                search_intent=OpenAICompatibleKeywordReviewer._text(data, "search_intent"),
                recommended_action=OpenAICompatibleKeywordReviewer._text(data, "recommended_action"),
                reason=OpenAICompatibleKeywordReviewer._text(data, "reason"),
                confidence=OpenAICompatibleKeywordReviewer._confidence(data),
            )
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise KeywordReviewProtocolError("AI keyword review returned invalid JSON.") from error
        return result

    @staticmethod
    def _boolean(data: Mapping[str, object], key: str) -> bool:
        value = data.get(key)
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be boolean")
        return value

    @staticmethod
    def _text(data: Mapping[str, object], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be text")
        return value.strip()

    @staticmethod
    def _confidence(data: Mapping[str, object]) -> float:
        value = data.get("confidence")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 1:
            raise ValueError("confidence must be from 0 to 1")
        return float(value)


class RuleBasedKeywordReviewer:
    """Safe local fallback when an AI provider has not been configured."""

    def review(self, *, seed_keyword: str, keyword: str, language: str) -> KeywordReview:
        seed = seed_keyword.casefold().strip()
        term = keyword.casefold().strip()
        seed_tokens = {part for part in seed.replace("-", " ").split() if len(part) > 2}
        same_topic = seed in term or bool(seed_tokens.intersection(term.replace("-", " ").split()))
        transactional_terms = {"buy", "price", "pricing", "service", "quote", "coupon", "discount", "购买", "价格", "报价", "优惠"}
        comparison_terms = {"best", "vs", "review", "compare", "alternative", "推荐", "对比", "评测", "哪个好"}
        question_terms = {"how", "what", "why", "guide", "教程", "怎么", "如何", "是什么"}
        words = set(term.replace("-", " ").split())
        if transactional_terms.intersection(words) or any(value in term for value in transactional_terms if len(value) > 1):
            intent, action = "transactional", "create_landing_page"
        elif comparison_terms.intersection(words) or any(value in term for value in comparison_terms if len(value) > 1):
            intent, action = "commercial", "create_comparison_article"
        elif question_terms.intersection(words) or any(value in term for value in question_terms if len(value) > 1):
            intent, action = "informational", "create_article"
        else:
            intent, action = "informational", "needs_manual_review"
        fit = same_topic and len(term) >= 2
        return KeywordReview(
            is_seo_content_fit=fit,
            same_topic_as_seed=same_topic,
            search_intent=intent,
            recommended_action=action if fit else "exclude_or_review",
            reason="Local rule review; configure an AI provider for semantic review.",
            confidence=0.65 if same_topic else 0.35,
        )
