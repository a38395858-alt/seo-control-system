"""Static browser contract for the seed-keyword Google Suggest workflow."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_FILE = PROJECT_ROOT / "frontend" / "src" / "App.tsx"


class SuggestExpansionWebTests(unittest.TestCase):
    def test_page_exposes_seed_keywords_and_market_controls(self) -> None:
        document = APP_FILE.read_text(encoding="utf-8")
        self.assertIn('id="seed-keywords"', document)
        self.assertIn('id="suggest-language"', document)
        self.assertIn('id="suggest-country"', document)

    def test_page_has_start_button_status_and_result_table(self) -> None:
        document = APP_FILE.read_text(encoding="utf-8")
        self.assertIn('id="start-suggest-expansion"', document)
        self.assertIn('id="suggest-keyword-table-body"', document)

    def test_page_exposes_a_live_debug_log_panel(self) -> None:
        document = APP_FILE.read_text(encoding="utf-8")
        self.assertIn('id="suggest-debug-log"', document)

    def test_page_exposes_the_keyword_difficulty_calculator(self) -> None:
        document = APP_FILE.read_text(encoding="utf-8")
        self.assertIn('id="calculate-keyword-score"', document)
        self.assertIn('id="keyword-score-result"', document)

    def test_react_disables_library_save_until_all_keywords_are_reviewed(self) -> None:
        document = APP_FILE.read_text(encoding="utf-8")
        self.assertIn('disabled={busy || !run || reviewedCount !== run.result.keywords.length}', document)

    def test_browser_script_uses_the_suggest_expansion_api(self) -> None:
        script = APP_FILE.read_text(encoding="utf-8") + (PROJECT_ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        self.assertIn("/api/suggest-expansions", script)
        self.assertIn("seed-keywords", script)
        self.assertIn("start-suggest-expansion", script)
        self.assertIn("debug_logs", script)
        self.assertIn("suggest-debug-log", script)
        self.assertIn("/api/keyword-opportunity-scores", script)
