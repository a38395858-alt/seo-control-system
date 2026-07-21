"""Static browser contracts for the staged content workspace."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "frontend" / "src" / "App.tsx")
STYLES = (ROOT / "frontend" / "src" / "styles.css")


class ContentSystemWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = APP.read_text(encoding="utf-8")
        self.styles = STYLES.read_text(encoding="utf-8")

    def test_content_workspace_has_a_staged_editor_and_asset_selector(self) -> None:
        self.assertIn('id="content-workspace"', self.app)
        self.assertIn('id="content-asset-list"', self.app)
        self.assertIn("内容工作流", self.app)

    def test_content_workspace_exposes_brief_outline_and_draft_stages(self) -> None:
        for label in ("内容 Brief", "文章大纲", "正文版本"):
            self.assertIn(label, self.app)
        self.assertIn("content-stage-step", self.app)
        self.assertNotIn('id="content-qa"', self.app)
        self.assertNotIn('"质量检查"', self.app)

    def test_content_workspace_uses_the_saved_chatgpt_model_and_three_step_copy(self) -> None:
        self.assertNotIn('contentSkillChatGptModel = "chat-latest"', self.app)
        self.assertIn("openai: aiProfiles.openai.model", self.app)
        self.assertNotIn("全文与 QA", self.app)
        self.assertNotIn("正文 → QA", self.app)

    def test_content_workspace_has_deliberate_visual_system(self) -> None:
        for selector in (".content-workspace", ".content-stage-rail", ".content-editor-card", ".content-asset-card"):
            self.assertIn(selector, self.styles)

    def test_content_workspace_can_use_asset_detail_and_existing_mvp_contracts(self) -> None:
        self.assertIn("getContentAsset", self.app)
        self.assertIn("createContentBrief", self.app)
        self.assertIn("createContentOutline", self.app)

    def test_target_length_is_unbounded_and_full_generation_is_available_without_outline(self) -> None:
        self.assertNotIn('min="800"', self.app)
        self.assertNotIn('max="3000"', self.app)
        self.assertIn('onClick={() => void generateStage("all")}', self.app)
        self.assertNotIn('disabled={!detail.outline || saving} onClick={() => void generateStage("all")}', self.app)

    def test_source_pack_keeps_each_article_as_one_input_for_outline_and_drafting(self) -> None:
        self.assertIn("sourcePack", self.app)
        self.assertIn("/\\n\\s*---+\\s*\\n/", self.app)

    def test_content_library_lists_all_generated_content(self) -> None:
        self.assertIn('path="/content-library"', self.app)
        self.assertIn("ContentLibrary", self.app)
        self.assertIn("所有内容", self.app)

    def test_content_detail_supports_version_and_markdown_html_switching(self) -> None:
        self.assertIn("selectedDraftVersion", self.app)
        self.assertIn("Markdown 源码", self.app)
        self.assertIn("HTML 预览", self.app)
        self.assertIn("renderMarkdownPreview", self.app)

    def test_content_library_excludes_assets_without_a_completed_draft(self) -> None:
        self.assertIn("completedAssets", self.app)
        self.assertIn(".filter((asset) => Boolean(asset.current_draft_id))", self.app)
        self.assertIn("暂无已完成正文", self.app)

    def test_content_library_initialization_keeps_the_project_when_a_secondary_load_fails(self) -> None:
        self.assertIn("Promise.allSettled", self.app)
        self.assertIn("restoreProjectAndAssets().catch(() => setAiStatus", self.app)
        self.assertNotIn("restoreProjectAndAssets().catch(() => { localStorage.removeItem(projectKey); setProjectId(null); });", self.app)
        self.assertIn("listContentLibrary", self.app)
        self.assertIn("/api/content-library", (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8"))

    def test_reader_safely_renders_semantic_markdown_including_tables(self) -> None:
        self.assertIn("escapeHtml", self.app)
        self.assertIn("renderInlineMarkdown", self.app)
        for tag in ("<h1>", "<h2>", "<h3>", "<p>", "<strong>", "<ul>", "<ol>", "<table>", "<thead>", "<tbody>", "<th>", "<td>"):
            self.assertIn(tag, self.app)
        self.assertIn(".content-reader", self.styles)
        self.assertIn(".markdown-preview table", self.styles)

    def test_content_reader_supports_word_count_and_standalone_html_download(self) -> None:
        for value in ("articleWordCount", "words ·", "downloadArticleHtml", "下载 HTML", "text/html;charset=utf-8"):
            self.assertIn(value, self.app)
        self.assertIn(".content-reader-actions", self.styles)

    def test_content_reader_has_an_article_scoped_authority_reference_footer(self) -> None:
        self.assertIn("ArticleAuthorityReferences", self.app)
        self.assertIn("authority_sources", self.app)
        self.assertIn("权威来源与验证依据", self.app)
        self.assertIn(".article-authority-references", self.styles)

    def test_content_library_supports_selective_and_bulk_asset_deletion(self) -> None:
        source = self.app + (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        self.assertIn("deleteContentAssets", source)
        self.assertIn('"/api/content-assets"', source)
        self.assertIn("selectedAssetIds", self.app)
        self.assertIn("删除已选内容", self.app)

    def test_compact_asset_cards_have_a_direct_stop_propagation_delete_control(self) -> None:
        for value in ("content-asset-delete", "onDelete([asset.id])", "event.stopPropagation()"):
            self.assertIn(value, self.app)
        for selector in (".content-asset-delete", ".content-asset-card.is-complete", ".content-asset-card.is-outline-ready"):
            self.assertIn(selector, self.styles)

    def test_content_generation_makes_the_selected_model_lock_and_failed_stage_visible(self) -> None:
        for value in ("content-provider-lock", "本次严格使用", "content-generation-log", "generation_jobs", "contentStageLabel"):
            self.assertIn(value, self.app)
        for selector in (".content-provider-lock", ".content-generation-log", ".generation-job.failed"):
            self.assertIn(selector, self.styles)

    def test_agent_platform_console_is_exposed_from_the_sidebar(self) -> None:
        self.assertIn('to="/projects"', self.app)
        self.assertIn("AgentPlatformConsole", self.app)
        self.assertIn("agent-console", self.styles)
