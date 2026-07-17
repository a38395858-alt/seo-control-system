"""Static React contract for the one-keyword, many-candidates title workspace."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_FILE = PROJECT_ROOT / "frontend" / "src" / "App.tsx"
API_FILE = PROJECT_ROOT / "frontend" / "src" / "api.ts"
STYLES_FILE = PROJECT_ROOT / "frontend" / "src" / "styles.css"


class TitleGenerationWebTests(unittest.TestCase):
    """Keep the browser affordances aligned with the title-generation API."""

    def setUp(self) -> None:
        self.app = APP_FILE.read_text(encoding="utf-8")
        self.api = API_FILE.read_text(encoding="utf-8")
        self.styles = STYLES_FILE.read_text(encoding="utf-8")

    def test_keyword_library_exposes_a_title_generation_entry(self) -> None:
        """A saved keyword must be able to enter its own title workspace."""
        self.assertIn('id="open-title-workspace"', self.app)
        self.assertIn("生成标题", self.app)

    def test_title_workspace_can_generate_and_display_candidate_titles(self) -> None:
        """One selected keyword has an explicit generator and candidate list."""
        for element_id in (
            "title-generation",
            "generate-title-candidates",
            "title-candidate-list",
        ):
            self.assertIn(f'id="{element_id}"', self.app)

    def test_title_workspace_shows_the_current_single_selected_title(self) -> None:
        """The UI must distinguish a candidate list from the one final choice."""
        self.assertIn('id="selected-title"', self.app)
        self.assertIn("selectTitleCandidate", self.app)

    def test_react_calls_the_title_job_candidate_and_selection_apis(self) -> None:
        """The UI contracts to the documented asynchronous title API surface."""
        source = self.app + self.api
        for endpoint in (
            "/api/title-generation-jobs",
            "/title-candidates",
            "/select",
            "/api/keywords/",
        ):
            self.assertIn(endpoint, source)

    def test_title_workspace_uses_ai_to_extract_serp_titles_without_manual_pasting(self) -> None:
        """The workspace requests AI SERP research and feeds the titles into generation."""
        source = self.app + self.api
        self.assertIn("researchSerpTitles", source)
        self.assertIn("/api/serp-title-research", source)
        self.assertIn("浏览器抓取 Google 前 20 自然标题", self.app)
        self.assertNotIn("同行标题参考（可选）", self.app)
        self.assertNotIn("openGoogleSearch", self.app)

    def test_title_workspace_can_use_browser_serp_extraction(self) -> None:
        source = self.app + self.api
        self.assertIn("researchBrowserSerpTitles", source)
        self.assertIn("/api/browser-serp-title-research", source)
        self.assertIn("浏览器抓取 Google 前 20 自然标题", self.app)

    def test_title_workspace_can_generate_three_candidates_per_ai_provider(self) -> None:
        source = self.app + self.api
        self.assertIn("generateMultiProviderTitles", source)
        self.assertIn("/api/multi-provider-title-generation-jobs", source)
        self.assertIn("三模型各生成 3 个标题", self.app)

    def test_title_cards_show_the_specific_ai_provider(self) -> None:
        self.assertIn("providerLabel", self.app)
        self.assertIn("provider-badge", self.app)
        self.assertIn("provider-chatgpt", self.styles)
        self.assertIn("provider-gemini", self.styles)
        self.assertIn("provider-deepseek", self.styles)

    def test_title_workspace_keeps_a_visible_saved_title_history(self) -> None:
        self.assertIn("最近入库标题", self.app)
        self.assertIn("生成后会自动写入标题库", self.app)
        self.assertIn("RecentTitleLibrary", self.app)

    def test_selected_title_library_entry_can_enter_content_generation(self) -> None:
        self.assertIn("加入内容生成", self.app)
        self.assertIn("createContentFromTitle", self.app)
        self.assertIn("onCreateContent", self.app)

    def test_title_library_can_select_a_candidate_before_content_creation(self) -> None:
        self.assertIn("选定标题", self.app)
        self.assertIn("selectLibraryTitle", self.app)
        self.assertIn("onSelectTitle", self.app)

    def test_content_page_does_not_offer_duplicate_asset_creation(self) -> None:
        self.assertIn("createdByTitle", self.app)
        self.assertIn("asset ?", self.app)
