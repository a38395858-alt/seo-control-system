"""Contracts for the model selected by the content-synthesis workflow."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.web import KeywordDiscoveryRequestHandler, create_server  # noqa: E402


class ContentModelSelectionTests(unittest.TestCase):
    def test_chatgpt_content_generation_uses_the_configured_gpt_5_4_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings_path = Path(temporary_directory) / "ai-settings.json"
            settings_path.write_text(json.dumps({"providers": {
                "openai": {"api_key": "test", "base_url": "https://openai.example/v1", "model": "gpt-5.4"},
                "gemini": {"api_key": "test", "base_url": "https://gemini.example/v1", "model": "gemini-test"},
            }}), encoding="utf-8")
            server = create_server("127.0.0.1", 0, database_path=Path(temporary_directory) / "content.sqlite3", ai_settings_path=settings_path)
            try:
                handler = object.__new__(KeywordDiscoveryRequestHandler)
                handler.server = server
                generator, provider, model = handler._content_generator({"provider": "openai"})
                self.assertEqual("openai", provider)
                self.assertEqual("gpt-5.4", model)
                self.assertEqual("gpt-5.4", generator.model)

                _generator, provider, model = handler._content_generator({"provider": "gemini"})
                self.assertEqual("gemini", provider)
                self.assertEqual("gemini-test", model)
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
