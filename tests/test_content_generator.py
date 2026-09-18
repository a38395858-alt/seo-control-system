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

from seo_control.application.content_generator import ContentGenerationProtocolError, OpenAICompatibleContentGenerator, PROMPT_VERSION, _stage_instruction  # noqa: E402
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

    def test_yes_no_outline_uses_ai_selected_h2_count_and_no_answer_chapter(self) -> None:
        title = "Can LED Strip Lights Be Used Outdoors? A Complete Guide"
        constraints = KeywordDiscoveryRequestHandler._outline_editorial_constraints(title)
        self.assertIn("Choose the H2 count", constraints["h2_count_rule"])
        excessive = [{"heading": f"Decision {index}"} for index in range(6)]
        self.assertIsNone(KeywordDiscoveryRequestHandler._outline_editorial_violation(title, excessive))
        answer_heading = [{"heading": "The Quick Answer"}, {"heading": "Select the right product"}, {"heading": "Install it"}, {"heading": "Maintain it"}]
        self.assertIn("separate answer H2", KeywordDiscoveryRequestHandler._outline_editorial_violation(title, answer_heading) or "")
        unsupported_precision = [
            {"heading": "Use it outdoors", "purpose": "Assess exposure.", "source_ids": ["competitor-1"], "key_points": ["Use IP65 for rain."]},
            {"heading": "Choose it", "purpose": "Read the product information.", "source_ids": ["competitor-2"], "key_points": ["Compare the documented rating."]},
            {"heading": "Install it", "purpose": "Follow the manual.", "source_ids": ["competitor-3"], "key_points": ["Use the supplied instructions."]},
            {"heading": "Maintain it", "purpose": "Inspect it.", "source_ids": ["competitor-4"], "key_points": ["Check for visible damage."]},
        ]
        self.assertIn("unsupported precise claim", KeywordDiscoveryRequestHandler._outline_editorial_violation(title, unsupported_precision) or "")
        cleaned = KeywordDiscoveryRequestHandler._remove_unverified_outline_precision(unsupported_precision)
        self.assertEqual(["Compare the documented rating."], cleaned[1]["key_points"])
        self.assertIsNone(KeywordDiscoveryRequestHandler._outline_editorial_violation(title, cleaned))

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

    def test_fallback_internal_link_plan_uses_only_a_supplied_unique_destination(self) -> None:
        plan = KeywordDiscoveryRequestHandler._fallback_internal_link_plan(
            [
                {"source_id": "gsc-anchor-2", "source_type": "gsc_anchor", "title": "GSC internal-link anchor: solar step lights", "target_url": "https://example.test/solar-step-lights"},
                {"source_id": "gsc-anchor-3", "source_type": "gsc_anchor", "title": "GSC internal-link anchor: concrete step lights", "target_url": "https://example.test/concrete-step-lights"},
            ],
            {"https://example.test/solar-step-lights"},
        )
        self.assertTrue(plan["use"])
        self.assertEqual("concrete step lights", plan["anchor_text"])
        self.assertEqual("https://example.test/concrete-step-lights", plan["target_url"])

    def test_authority_reference_footer_requires_used_permitted_authority_sources(self) -> None:
        markdown = KeywordDiscoveryRequestHandler._append_authority_reference_block(
            "# Article\n\nA supported technical point.",
            sources=[
                {"source_id": "authority-doe", "source_type": "authority_source", "authority_level": "authoritative", "publisher": "U.S. Department of Energy", "title": "PV Maintenance Guide", "url": "https://energy.gov/pv-maintenance"},
                {"source_id": "authority-wiki", "source_type": "authority_source", "authority_level": "supporting", "publisher": "Wikipedia", "title": "Unreviewed background", "url": "https://en.wikipedia.org/wiki/Test"},
            ],
            source_ids=["authority-doe", "authority-wiki"],
        )
        self.assertIn("## 权威参考与验证链接", markdown)
        self.assertIn("**U.S. Department of Energy** – PV Maintenance Guide [链接](https://energy.gov/pv-maintenance)", markdown)
        self.assertIn("备用验证方式：搜索完整标题", markdown)
        self.assertNotIn("Wikipedia", markdown)

    def test_authority_search_routes_maintenance_claims_to_relevant_institutions(self) -> None:
        tasks = KeywordDiscoveryRequestHandler._authority_google_tasks(
            "## How to Clean Solar Panels and Fixtures\n\n## Battery Checks\n\n## Inspect the Housing and IP Rating",
            "Maintaining Solar Garden Lights",
            "led garden lights outdoor solar",
        )
        queries = [task["query"] for task in tasks]
        self.assertTrue(any("site:nrel.gov" in query for query in queries))
        self.assertTrue(any("site:batteryuniversity.com" in query for query in queries))
        self.assertTrue(any("site:iec.ch" in query for query in queries))

    def test_authority_relevance_recognizes_supported_technical_equivalents(self) -> None:
        relevant, _reason = KeywordDiscoveryRequestHandler._authority_source_relevance(
            keyword="led garden lights outdoor solar",
            article_title="Maintaining Solar Garden Lights",
            section_heading="How to Clean Solar Panels and Fixtures",
            source_title="Photovoltaic Soiling Research",
            source_content="Photovoltaic soiling affects maintenance decisions for solar panels.",
        )
        self.assertTrue(relevant)

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

    def test_full_article_uses_a_longer_timeout_than_small_stages(self) -> None:
        generator = OpenAICompatibleContentGenerator("test-key", "https://example.test/v1", "gpt-test", provider="deepseek", timeout=90)
        response = MagicMock()
        response.read.return_value = json.dumps({"choices": [{"message": {"content": '{"title":"Test","meta_description":"","markdown":"# Test","sources_used":[],"claims_used":[],"verify":[]}'}}]}).encode("utf-8")
        context = MagicMock(); context.__enter__.return_value = response; context.__exit__.return_value = False
        with patch("seo_control.application.content_generator.urlopen", return_value=context) as request_mock:
            result = generator.run_stage(stage="full_article", data={})
        self.assertEqual("# Test", result["markdown"])
        self.assertEqual(240.0, request_mock.call_args.kwargs["timeout"])

    def test_full_article_source_context_is_bounded_without_losing_source_ids(self) -> None:
        sources = [
            {"source_id": "authority-1", "source_type": "authority_source", "content": "authority " * 8_000},
            {"source_id": "competitor-2", "source_type": "competitor_page", "content": "competitor " * 8_000},
            {"source_id": "company-3", "source_type": "company_knowledge", "content": "company " * 8_000},
        ]

        compacted = KeywordDiscoveryRequestHandler._compact_full_article_sources(sources)

        self.assertEqual(["authority-1", "competitor-2", "company-3"], [item["source_id"] for item in compacted])
        self.assertLessEqual(sum(len(item["content"]) for item in compacted), 24_000)
        self.assertLessEqual(len(compacted[0]["content"]), 6_002)
        self.assertLessEqual(len(compacted[1]["content"]), 2_502)

    def test_full_article_prompt_requires_focus_and_us_reader_conventions(self) -> None:
        instruction = _stage_instruction("full_article")

        self.assertIn("not an encyclopedia", instruction)
        self.assertIn("country_code is US", instruction)
        self.assertIn("American English", instruction)
        self.assertIn("U.S. customary units first", instruction)

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
        generator = OpenAICompatibleContentGenerator("test-key", "https://example.test/v1", "gpt-5.4", provider="openai", custom_instruction="Use a restrained engineering voice.")
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
        self.assertEqual("people_first_full_article_v28", PROMPT_VERSION)
        self.assertIn("company_knowledge", contract["instruction"])
        self.assertIn("exact public product/company URL", contract["instruction"])
        self.assertIn("company_context_plan", contract["output_schema"])
        self.assertEqual("Use a restrained engineering voice.", contract["project_supplemental_instruction"])
        self.assertIn("does not conflict", contract["project_instruction_policy"])

    def test_deepseek_request_enables_high_effort_thinking_without_temperature(self) -> None:
        generator = OpenAICompatibleContentGenerator("test-key", "https://api.deepseek.com", "deepseek-v4-flash", provider="deepseek")
        response = MagicMock()
        response.read.return_value = json.dumps({"choices": [{"message": {"content": '{"tags":["Stair Lighting"]}'}}]}).encode("utf-8")
        context = MagicMock(); context.__enter__.return_value = response; context.__exit__.return_value = False
        with patch("seo_control.application.content_generator.urlopen", return_value=context) as request_mock:
            generator.run_stage(stage="content_tags", data={})

        request = request_mock.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual({"type": "enabled"}, payload["thinking"])
        self.assertEqual("high", payload["reasoning_effort"])
        self.assertNotIn("temperature", payload)

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
