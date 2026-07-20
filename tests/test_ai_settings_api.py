"""Red-first contract for local AI connection settings."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.web import create_server  # noqa: E402


class AiSettingsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        self.server = create_server("127.0.0.1", 0, database_path=path / "seo.sqlite3", ai_settings_path=path / "ai-settings.json")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request_json(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        request = Request(self.base_url + path, data=None if payload is None else json.dumps(payload).encode("utf-8"), headers={} if payload is None else {"Content-Type": "application/json"}, method=method)
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_saves_a_local_connection_without_returning_the_api_key(self) -> None:
        status, saved = self.request_json("POST", "/api/settings/ai", {"api_key": "secret-key", "base_url": "https://api.example.com/v1", "model": "test-model"})
        self.assertEqual(200, status)
        self.assertTrue(saved["configured"])
        self.assertNotIn("api_key", saved)

        status, current = self.request_json("GET", "/api/settings/ai")
        self.assertEqual(200, status)
        self.assertEqual("https://api.example.com/v1", current["base_url"])
        self.assertEqual("test-model", current["model"])
        self.assertNotIn("secret-key", json.dumps(current))

    def test_identifies_gemini_openai_compatibility_endpoint_as_gemini(self) -> None:
        status, saved = self.request_json(
            "POST",
            "/api/settings/ai",
            {
                "api_key": "gemini-secret",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-2.5-flash",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("gemini", saved["provider"])

        status, current = self.request_json("GET", "/api/settings/ai")
        self.assertEqual(200, status)
        self.assertEqual("gemini", current["provider"])

    def test_saves_three_provider_profiles_and_separate_feature_assignments(self) -> None:
        status, saved = self.request_json(
            "POST",
            "/api/settings/ai",
            {
                "providers": {
                    "openai": {"api_key": "openai-secret", "base_url": "https://api.openai.com/v1", "model": "gpt-5.5"},
                    "gemini": {"api_key": "gemini-secret", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-flash"},
                    "deepseek": {"api_key": "deepseek-secret", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro"},
                },
                "assignments": {"keyword_review": "gemini", "title_generation": "deepseek"},
            },
        )
        self.assertEqual(200, status)
        self.assertEqual({"keyword_review": "gemini", "title_generation": "deepseek", "content_generation": "openai"}, saved["assignments"])
        self.assertEqual("gemini-2.5-flash", saved["providers"]["gemini"]["model"])
        self.assertTrue(saved["providers"]["openai"]["configured"])
        self.assertNotIn("openai-secret", json.dumps(saved))
        self.assertNotIn("gemini-secret", json.dumps(saved))
        self.assertNotIn("deepseek-secret", json.dumps(saved))

        status, saved_again = self.request_json(
            "POST",
            "/api/settings/ai",
            {
                "providers": {
                    "openai": {"api_key": "", "base_url": "https://api.openai.com/v1", "model": "gpt-5.5"},
                    "gemini": {"api_key": "", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-flash"},
                    "deepseek": {"api_key": "", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro"},
                },
                "assignments": {"keyword_review": "gemini", "title_generation": "deepseek"},
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(saved_again["providers"]["openai"]["configured"])
        self.assertTrue(saved_again["providers"]["gemini"]["configured"])
        self.assertTrue(saved_again["providers"]["deepseek"]["configured"])

    def test_saving_one_provider_keeps_the_other_saved_provider_profiles(self) -> None:
        self.request_json(
            "POST",
            "/api/settings/ai",
            {
                "providers": {
                    "openai": {"api_key": "openai-secret", "base_url": "https://api.openai.com/v1", "model": "gpt-5.5"},
                    "gemini": {"api_key": "gemini-secret", "base_url": "https://gemini.example/v1", "model": "gemini-test"},
                },
                "assignments": {"keyword_review": "openai", "title_generation": "gemini"},
            },
        )

        status, saved = self.request_json(
            "POST",
            "/api/settings/ai",
            {
                "providers": {
                    "openai": {"api_key": "", "base_url": "https://proxy.example/v1", "model": "gpt-5.4"},
                },
                "assignments": {"keyword_review": "openai", "title_generation": "gemini"},
            },
        )

        self.assertEqual(200, status)
        self.assertEqual("https://proxy.example/v1", saved["providers"]["openai"]["base_url"])
        self.assertEqual("gpt-5.4", saved["providers"]["openai"]["model"])
        self.assertTrue(saved["providers"]["gemini"]["configured"])

    def test_normalizes_a_full_chat_completions_url_to_a_base_url(self) -> None:
        status, saved = self.request_json(
            "POST",
            "/api/settings/ai",
            {
                "providers": {
                    "gemini": {
                        "api_key": "gemini-secret",
                        "base_url": "http://proxy.example/v1/chat/completions",
                        "model": "gemini-test",
                    },
                },
                "assignments": {"keyword_review": "gemini", "title_generation": "gemini"},
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("http://proxy.example/v1", saved["providers"]["gemini"]["base_url"])

    def test_tests_the_current_connection_without_persisting_content(self) -> None:
        self.request_json("POST", "/api/settings/ai", {"api_key": "secret-key", "base_url": "https://api.example.com/v1", "model": "test-model"})

        class FakeGenerator:
            def generate(self, **_request: object) -> str:
                return '{"candidates":[{"title":"Test title"}]}'

        self.server.title_generator = FakeGenerator()
        status, result = self.request_json("POST", "/api/settings/ai/test", {})
        self.assertEqual(200, status)
        self.assertEqual("connected", result["status"])
        self.assertNotIn("secret-key", json.dumps(result))

    def test_tests_one_saved_provider_without_requiring_the_key_to_remain_in_the_form(self) -> None:
        self.request_json(
            "POST",
            "/api/settings/ai",
            {
                "providers": {
                    "deepseek": {"api_key": "deepseek-secret", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro"},
                },
                "assignments": {"keyword_review": "openai", "title_generation": "openai"},
            },
        )

        class FakeGenerator:
            def generate(self, **_request: object) -> str:
                return '{"candidates":[{"title":"Test title"}]}'

        with patch("seo_control.web.OpenAICompatibleTitleGenerator", return_value=FakeGenerator()):
            status, result = self.request_json("POST", "/api/settings/ai/test", {"provider": "deepseek"})
        self.assertEqual(200, status)
        self.assertEqual("connected", result["status"])
        self.assertEqual("deepseek", result["provider"])
        self.assertEqual("deepseek-v4-pro", result["model"])
        self.assertNotIn("deepseek-secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
