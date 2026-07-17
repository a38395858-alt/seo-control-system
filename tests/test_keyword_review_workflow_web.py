"""Contract tests for keyword-review modes and automatic persistence."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_FILE = PROJECT_ROOT / "frontend" / "src" / "App.tsx"


class KeywordReviewWorkflowWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = APP_FILE.read_text(encoding="utf-8")

    def test_review_workspace_offers_fast_and_hybrid_modes(self) -> None:
        self.assertIn('useState<"fast" | "hybrid">("hybrid")', self.app)
        self.assertIn('value={reviewMode}', self.app)
        self.assertIn('["fast", "快速：仅本地规则（即时）"]', self.app)
        self.assertIn('["hybrid", "混合：规则预筛 + AI 精审（推荐）"]', self.app)

    def test_review_request_sends_selected_mode(self) -> None:
        self.assertIn('language: run.language, mode: reviewMode', self.app)
        self.assertIn('本地规则，零 AI 请求', self.app)

    def test_completed_review_automatically_persists_approved_keywords(self) -> None:
        self.assertIn('const persistReviewedKeywords = async', self.app)
        self.assertIn('await persistReviewedKeywords(run, next);', self.app)
        self.assertIn('审核完成并已入库：', self.app)

    def test_review_panel_no_longer_requires_manual_save_button(self) -> None:
        self.assertNotIn('审核后加入关键词库', self.app)


if __name__ == "__main__":
    unittest.main()
