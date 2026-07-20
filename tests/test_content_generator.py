"""Adapter error contracts for evidence-grounded content generation."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.content_generator import ContentGenerationProtocolError, OpenAICompatibleContentGenerator, PROMPT_VERSION  # noqa: E402


class ContentGeneratorTests(unittest.TestCase):
    def test_timeout_reports_the_stage_and_timeout_limit_without_provider_fallback(self) -> None:
        generator = OpenAICompatibleContentGenerator("test-key", "https://example.test/v1", "gpt-test", provider="openai", timeout=90)
        with patch("seo_control.application.content_generator.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(ContentGenerationProtocolError, r"assembly timed out after 90 seconds"):
                generator.run_stage(stage="assembly", data={})

    def test_stage_request_sends_the_versioned_instruction_and_json_contract(self) -> None:
        generator = OpenAICompatibleContentGenerator("test-key", "https://example.test/v1", "gpt-5.4", provider="openai")
        response = MagicMock()
        response.read.return_value = json.dumps({"choices": [{"message": {"content": '{"intro_brief":"","sections":[],"conclusion_brief":"","cta_placement":""}'}}]}).encode("utf-8")
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with patch("seo_control.application.content_generator.urlopen", return_value=context) as request_mock:
            generator.run_stage(stage="outline", data={"semantic": {}, "metadata": {}})

        request = request_mock.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        contract = json.loads(payload["messages"][1]["content"])
        self.assertEqual("gpt-5.4", payload["model"])
        self.assertEqual("outline", contract["stage"])
        self.assertIn("title promise", contract["instruction"])
        self.assertIn("source IDs", contract["instruction"])
        self.assertIn("Source column", contract["instruction"])
        self.assertIn("sections", contract["output_schema"])
        self.assertEqual("content_seo_eeat_v2", PROMPT_VERSION)


if __name__ == "__main__":
    unittest.main()
