"""Front-end contract tests for the keyword AI review entry point.

These tests deliberately describe the UI/API contract before the feature is
implemented.  They keep the first increment small: the expanded-keyword view
must expose one review action, a visible status target, and use the agreed
endpoint and result fields.
"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "web"


def _read_web_file(filename: str) -> str:
    return (WEB_ROOT / filename).read_text(encoding="utf-8")


def test_expanded_keywords_view_exposes_ai_review_controls() -> None:
    """Users can start reviewing the displayed expanded keywords and see state."""
    html = _read_web_file("index.html")

    assert 'id="review-expanded-keywords"' in html
    assert 'id="ai-review-status"' in html


def test_ai_keyword_review_client_uses_agreed_endpoint_and_result_fields() -> None:
    """The browser sends review work to the API and renders its decision fields."""
    javascript = _read_web_file("app.js")

    assert "/api/ai-keyword-reviews" in javascript
    assert "is_seo_content_fit" in javascript
    assert "same_topic_as_seed" in javascript
    assert "recommended_action" in javascript
