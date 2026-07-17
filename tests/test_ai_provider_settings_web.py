"""Browser contract for selecting the global AI provider."""

from pathlib import Path
import unittest


APP_FILE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.tsx"


class AiProviderSettingsWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = APP_FILE.read_text(encoding="utf-8")

    def test_settings_render_three_independent_provider_cards(self) -> None:
        self.assertIn("ProviderCard", self.app)
        self.assertIn('provider="openai"', self.app)
        self.assertIn('provider="gemini"', self.app)
        self.assertIn('provider="deepseek"', self.app)

    def test_provider_cards_and_feature_assignments_keep_provider_choices_separate(self) -> None:
        self.assertIn("const aiProviderPresets", self.app)
        self.assertIn("aiAssignments", self.app)
        self.assertIn("keyword_review", self.app)
        self.assertIn("title_generation", self.app)
        self.assertIn("https://api.openai.com/v1", self.app)
        self.assertIn("https://generativelanguage.googleapis.com/v1beta/openai", self.app)

    def test_card_connection_test_uses_the_current_unsaved_profile(self) -> None:
        self.assertIn("profile.apiKey ? { provider, config: profile }", self.app)

    def test_card_connection_test_falls_back_to_its_saved_profile_when_key_input_is_empty(self) -> None:
        self.assertIn("profile.apiKey ? { provider, config: profile } : { provider }", self.app)

    def test_each_provider_card_has_its_own_save_action(self) -> None:
        self.assertIn("onSave={saveAiProvider}", self.app)
        self.assertIn("保存 {title}", self.app)
        self.assertIn("const saveAiProvider = async (provider: AiProvider)", self.app)


if __name__ == "__main__":
    unittest.main()
