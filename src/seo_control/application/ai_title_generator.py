"""US-market SEO title generators with an OpenAI-compatible adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TitleGenerationProtocolError(RuntimeError):
    """Raised when the configured title provider cannot return usable JSON."""


@dataclass(frozen=True)
class TitleGenerationRequest:
    keyword: str
    locale: str
    count: int
    search_intent: str | None = None
    category: str | None = None
    title_type: str | None = None


class OpenAICompatibleTitleGenerator:
    """Generate JSON title candidates through an OpenAI-compatible chat API."""

    def __init__(self, api_key: str, base_url: str, model: str, *, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def generate(self, **request: Any) -> str:
        payload = {
            "model": self._model,
            "temperature": 0.55,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You write SEO article title candidates. Return JSON only with a candidates array. "
                        "For en-US, use natural American English search language, avoid clickbait and unsupported claims. "
                        "When competitor_titles are supplied, use them only to infer search intent and content patterns; never copy or lightly rewrite a competitor title. "
                        "Each item needs title, title_type, primary_keyword_included, search_intent, and reason."
                    ),
                },
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
        }
        http_request = Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"},
            method="POST",
        )
        try:
            with urlopen(http_request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError) as error:
            raise TitleGenerationProtocolError("AI title generation request failed.") from error
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise TitleGenerationProtocolError("AI title generation returned an invalid response.") from error
        if not isinstance(content, str):
            raise TitleGenerationProtocolError("AI title generation returned non-text content.")
        return content

    def research_serp_titles(self, *, keyword: str, locale: str) -> str:
        """Ask a search-capable configured model for a structured SERP title snapshot."""
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a Google SERP research assistant. Use live Google search capability when it is available. "
                        "Return JSON only: {\"titles\":[{\"rank\":1,\"title\":\"...\",\"source\":\"domain or publisher\"}]}. "
                        "Return up to the first 20 organic English-US Google result page titles, in ranking order. "
                        "Exclude ads, AI overviews, videos, and generated suggestions. Never invent rankings or titles: "
                        "if live search is unavailable, return {\"titles\":[],\"warning\":\"live Google search is unavailable\"}."
                    ),
                },
                {"role": "user", "content": f"抓取谷歌关键词 {keyword} 前20排名标题。目标市场：{locale}。"},
            ],
        }
        http_request = Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"},
            method="POST",
        )
        try:
            with urlopen(http_request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
        except (HTTPError, URLError, OSError, ValueError, KeyError, IndexError, TypeError) as error:
            raise TitleGenerationProtocolError("AI SERP title research request failed.") from error
        if not isinstance(content, str):
            raise TitleGenerationProtocolError("AI SERP title research returned non-text content.")
        return content


class RuleBasedTitleGenerator:
    """Small offline fallback that keeps the local workspace usable without an API key."""

    def generate(self, **request: Any) -> str:
        keyword = str(request["keyword"]).strip()
        display_keyword = self._display_keyword(keyword)
        title_type = str(request.get("title_type") or "").strip().lower()
        intent = str(request.get("search_intent") or "informational").strip().lower()
        templates = self._templates(display_keyword, intent, title_type)
        count = max(1, min(int(request.get("count", 8)), len(templates)))
        candidates = [
            {
                "title": title,
                "title_type": kind,
                "primary_keyword_included": keyword.casefold() in title.casefold(),
                "search_intent": intent,
                "reason": reason,
            }
            for title, kind, reason in templates[:count]
        ]
        return json.dumps({"candidates": candidates}, ensure_ascii=False)

    @staticmethod
    def _templates(keyword: str, intent: str, title_type: str) -> list[tuple[str, str, str]]:
        if title_type == "comparison" or intent == "commercial":
            return [
                (f"{keyword}: Features to Compare Before You Buy", "comparison", "Uses a direct US buyer-guide angle."),
                (f"How to Choose {keyword}: A Practical Buyer’s Guide", "comparison", "Explains a decision process instead of making unsupported claims."),
                (f"{keyword}: Costs, Features, and What to Look For", "comparison", "Focuses on common comparison criteria."),
                (f"{keyword}: Which Option Is Right for Your Needs?", "comparison", "Uses natural American decision language."),
                (f"{keyword}: A Side-by-Side Comparison Guide", "comparison", "Sets up a concrete comparison article."),
                (f"{keyword}: What Small Businesses Should Compare", "comparison", "Names a common buyer audience and task."),
                (f"{keyword}: A Checklist for Smarter Decisions", "comparison", "Offers an actionable decision framework."),
                (f"{keyword}: Key Questions to Ask Before You Buy", "comparison", "Matches pre-purchase research behavior."),
            ]
        if title_type == "transactional" or intent == "transactional":
            return [
                (f"{keyword}: Pricing, Options, and What’s Included", "transactional", "Sets clear expectations for a purchase-intent search."),
                (f"How to Choose {keyword} for Your Business", "transactional", "Connects the offer to a business use case."),
                (f"{keyword}: A Buyer’s Guide for First-Time Customers", "transactional", "Keeps a helpful, non-clickbait commercial tone."),
                (f"{keyword}: Questions to Ask Before You Buy", "transactional", "Supports a high-intent evaluation journey."),
                (f"{keyword}: How to Find the Right Option for Your Needs", "transactional", "Uses a direct US buyer-oriented framing."),
                (f"{keyword}: Features and Services to Compare", "transactional", "Prepares buyers for a commercial comparison."),
                (f"{keyword}: A Straightforward Guide to Getting Started", "transactional", "Keeps the title useful after the purchase decision."),
                (f"{keyword}: What to Know Before Choosing a Provider", "transactional", "Avoids unsupported price or performance promises."),
            ]
        return [
            (f"How to Choose {keyword}: A Step-by-Step Guide", "tutorial", "Matches a practical informational search."),
            (f"{keyword}: What It Is, How It Works, and When to Use It", "guide", "Explains the topic and its use cases."),
            (f"{keyword}: A Practical Guide for Beginners", "tutorial", "Uses a clear learning-oriented promise."),
            (f"{keyword}: Common Questions Answered", "faq", "Fits question-led informational searches."),
            (f"Getting Started With {keyword}: What You Need to Know", "tutorial", "Frames the topic for a new US reader."),
            (f"{keyword}: A Simple Checklist for First-Time Users", "checklist", "Gives the reader an actionable content promise."),
            (f"{keyword}: Benefits, Use Cases, and Common Mistakes", "guide", "Covers practical evaluation questions."),
            (f"{keyword}: Tips for Choosing the Right Approach", "guide", "Keeps the benefit and decision context specific."),
        ]

    @staticmethod
    def _display_keyword(keyword: str) -> str:
        acronyms = {"ai", "api", "b2b", "b2c", "crm", "cms", "saas", "seo", "ux", "ui"}
        return " ".join(word.upper() if word.casefold() in acronyms else word[:1].upper() + word[1:] for word in keyword.split())
