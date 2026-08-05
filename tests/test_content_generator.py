"""Adapter error contracts for evidence-grounded content generation."""

from __future__ import annotations

import json
import ssl
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.content_generator import ContentGenerationProtocolError, OpenAICompatibleContentGenerator, PROMPT_VERSION  # noqa: E402
from seo_control.web import KeywordDiscoveryRequestHandler  # noqa: E402


class ContentGeneratorTests(unittest.TestCase):
    def test_outline_recovery_drops_title_echo_and_duplicate_h2s(self) -> None:
        sections = [
            {"heading": "Are LED Stair Lights Waterproof?", "purpose": "accidental H1 echo"},
            {"heading": "How to Check an IP Rating", "purpose": "evaluation"},
            {"heading": "How to Check an IP Rating", "purpose": "duplicate"},
            {"heading": "Installation Risks to Avoid", "purpose": "implementation"},
        ]
        result = KeywordDiscoveryRequestHandler._deduplicate_ai_outline_sections("Are LED Stair Lights Waterproof?", sections)
        self.assertEqual(["How to Check an IP Rating", "Installation Risks to Avoid"], [item["heading"] for item in result])

    def test_outline_keeps_the_promised_numbered_listicle_chapter(self) -> None:
        title = "10 Inspiring Ideas for LED Stair Lights Outdoor"
        sections = [
            {"heading": title, "purpose": "Deliver ten distinct lighting ideas."},
            {"heading": "How to Choose Between the Ideas", "purpose": "Turn inspiration into a decision."},
        ]
        result = KeywordDiscoveryRequestHandler._deduplicate_ai_outline_sections(title, sections)
        self.assertEqual([title, "How to Choose Between the Ideas"], [item["heading"] for item in result])
        self.assertEqual(10, KeywordDiscoveryRequestHandler._numbered_listicle_count(title))
        self.assertEqual(10, KeywordDiscoveryRequestHandler._numbered_listicle_count("10 个户外 LED 楼梯灯的创意"))

    def test_company_knowledge_is_assigned_to_only_relevant_h2s(self) -> None:
        assignments = KeywordDiscoveryRequestHandler._company_context_for_sections(
            article_text="Industrial LED flood light selection guide",
            sections=[
                {"position": 1, "heading": "How to choose industrial flood lights", "purpose": "Compare installation and selection criteria", "key_points": ["Evaluate mounting"]},
                {"position": 2, "heading": "Maintenance planning", "purpose": "Plan lifecycle checks", "key_points": []},
                {"position": 3, "heading": "Frequently asked questions", "purpose": "Answer common questions", "key_points": []},
            ],
            sources=[
                {"source_id": "company-knowledge-7", "source_type": "company_knowledge", "title": "Industrial LED flood light product page", "url": "https://example.test/industrial-led-flood-lights", "content": "Industrial LED flood lights support mounting selection and product specifications."},
                {"source_id": "company-knowledge-8", "source_type": "company_knowledge", "title": "Unrelated office furniture", "content": "Office desks and chairs."},
            ],
        )
        assigned_ids = [source["source_id"] for values in assignments.values() for source in values]
        self.assertEqual(["company-knowledge-7"], assigned_ids)
        self.assertEqual([1], list(assignments))

    def test_primary_keyword_emphasis_is_normalised_to_one_body_occurrence(self) -> None:
        markdown = "# How Does an Automatic LED Stair Light Controller Work?\n\nAn **automatic LED stair light controller** coordinates the system.\n\n## Components\n\nAn **automatic LED stair light controller** evaluates inputs."
        result = KeywordDiscoveryRequestHandler._normalise_primary_keyword_emphasis(markdown, "automatic led stair light controller")
        self.assertEqual(1, KeywordDiscoveryRequestHandler._bold_primary_keyword_count(result, "automatic led stair light controller"))
        self.assertIn("# How Does an Automatic LED Stair Light Controller Work?", result)
        self.assertIn("An automatic LED stair light controller evaluates inputs.", result)

    def test_ai_outline_plan_keeps_only_verified_company_source_and_exact_url(self) -> None:
        sections = [{"heading": "How to choose flood lights", "purpose": "Evaluate product fit"}, {"heading": "Maintenance", "purpose": "Plan checks"}]
        planned = KeywordDiscoveryRequestHandler._apply_ai_company_context_plan(
            sections,
            {"assignments": [
                {"section_heading": "How to choose flood lights", "source_ids": ["company-knowledge-7"], "factual_role": "Product fit", "link_url": "https://example.test/flood-lights"},
                {"section_heading": "Maintenance", "source_ids": ["untrusted-id"], "factual_role": "Ignore", "link_url": "https://bad.test/"},
            ]},
            [{"source_id": "company-knowledge-7", "source_type": "company_knowledge", "url": "https://example.test/flood-lights", "content": "Verified product facts."}],
        )
        self.assertEqual(["company-knowledge-7"], planned[0]["company_context_source_ids"])
        self.assertEqual("https://example.test/flood-lights", planned[0]["company_context_link_url"])
        self.assertEqual([], planned[1].get("company_context_source_ids", []))

    def test_timeout_reports_the_stage_and_timeout_limit_without_provider_fallback(self) -> None:
        generator = OpenAICompatibleContentGenerator("test-key", "https://example.test/v1", "gpt-test", provider="openai", timeout=90)
        with patch("seo_control.application.content_generator.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(ContentGenerationProtocolError, r"assembly timed out after 90 seconds"):
                generator.run_stage(stage="assembly", data={})

    def test_transient_network_error_is_retried_before_success(self) -> None:
        generator = OpenAICompatibleContentGenerator("test-key", "https://example.test/v1", "gpt-test", provider="openai")
        response = MagicMock()
        response.read.return_value = json.dumps({"choices": [{"message": {"content": '{"tags":["Stair Lighting","Guide"]}'}}]}).encode("utf-8")
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with patch("seo_control.application.content_generator.time.sleep"), patch(
            "seo_control.application.content_generator.urlopen",
            side_effect=[URLError("connection reset"), context],
        ) as request_mock:
            result = generator.run_stage(stage="content_tags", data={})

        self.assertEqual(["Stair Lighting", "Guide"], result["tags"])
        self.assertEqual(2, request_mock.call_count)

    def test_deepseek_tls_eof_retries_on_a_new_short_lived_connection(self) -> None:
        generator = OpenAICompatibleContentGenerator("test-key", "https://example.test/v1", "deepseek-test", provider="deepseek")
        response = MagicMock()
        response.read.return_value = json.dumps({"choices": [{"message": {"content": '{"tags":["Stair Lighting"]}'}}]}).encode("utf-8")
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with patch("seo_control.application.content_generator.time.sleep"), patch(
            "seo_control.application.content_generator.urlopen",
            side_effect=[URLError(ssl.SSLEOFError(8, "EOF occurred in violation of protocol")), context],
        ) as request_mock:
            result = generator.run_stage(stage="content_tags", data={})

        self.assertEqual(["Stair Lighting"], result["tags"])
        self.assertEqual(2, request_mock.call_count)
        retry_request = request_mock.call_args.args[0]
        self.assertEqual("close", retry_request.get_header("Connection"))

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
        self.assertIn("reader-facing Source or Verification column", contract["instruction"])
        self.assertIn("sections", contract["output_schema"])
        self.assertEqual("people_first_full_article_v21", PROMPT_VERSION)
        self.assertIn("company_knowledge", contract["instruction"])
        self.assertIn("exact public product/company URL", contract["instruction"])
        self.assertIn("company_context_plan", contract["output_schema"])

    def test_stage_request_accepts_a_json_object_wrapped_in_a_markdown_fence(self) -> None:
        generator = OpenAICompatibleContentGenerator("test-key", "https://example.test/v1", "gemini-test", provider="gemini")
        response = MagicMock()
        response.read.return_value = json.dumps({"choices": [{"message": {"content": "```json\n{\"source_candidates\": []}\n```"}}]}).encode("utf-8")
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with patch("seo_control.application.content_generator.urlopen", return_value=context):
            result = generator.run_stage(stage="authority_research_plan", data={"title": "Test"})
        self.assertEqual({"source_candidates": []}, result)


if __name__ == "__main__":
    unittest.main()
