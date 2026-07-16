"""Static contract for the expanded-keyword AI review UI."""

from pathlib import Path
import unittest


class AiKeywordReviewWebTests(unittest.TestCase):
    def test_page_and_client_expose_review_contract(self) -> None:
        root = Path(__file__).resolve().parents[1] / "frontend" / "src"
        script = (root / "App.tsx").read_text(encoding="utf-8") + (root / "api.ts").read_text(encoding="utf-8")
        html = script
        self.assertIn('id="review-expanded-keywords"', html)
        self.assertIn('id="ai-review-status"', html)
        for value in ("/api/ai-keyword-reviews", "is_seo_content_fit", "same_topic_as_seed", "recommended_action"):
            self.assertIn(value, script)
