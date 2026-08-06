"""Prompt contracts for industry-agnostic, evidence-led SEO content."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.content_generator import SYSTEM_PROMPT, _stage_instruction  # noqa: E402


class ContentPromptQualityTests(unittest.TestCase):
    def test_universal_prompt_requires_people_first_eeat_and_no_fabricated_expertise(self) -> None:
        for text in ("industry-agnostic", "E-E-A-T", "people-first", "Do not fabricate personal experience"):
            self.assertIn(text, SYSTEM_PROMPT)

    def test_outline_prompt_requires_title_promise_source_and_decision_coverage(self) -> None:
        instruction = _stage_instruction("outline")
        for text in ("title promise", "source IDs", "decision order", "keyword_requirements", "depth_requirements"):
            self.assertIn(text, instruction)

    def test_competitor_evidence_is_internal_and_step_titles_require_a_checklist(self) -> None:
        self.assertIn("internal evidence keys", SYSTEM_PROMPT)
        self.assertIn("Never print an internal source ID", SYSTEM_PROMPT)
        self.assertIn("dedicated ordered process section", _stage_instruction("outline"))
        self.assertIn("Never print source IDs", _stage_instruction("section"))
        self.assertIn("strictly forbidden in all reader-facing text", _stage_instruction("assembly"))

    def test_prompt_requires_multi_article_depth_without_a_fixed_length_or_h2_count(self) -> None:
        self.assertIn("deep original synthesis of multiple supplied articles", SYSTEM_PROMPT)
        self.assertIn("not a short summary", _stage_instruction("competitor_analysis"))
        self.assertIn("one substantial chapter", _stage_instruction("section"))
        self.assertIn("must be preserved locally", _stage_instruction("assembly"))

    def test_full_article_prompt_receives_each_h2_requirement_before_prose(self) -> None:
        from seo_control.application.content_generator import _schema_for  # noqa: E402

        instruction = _stage_instruction("full_article")
        for text in ("one response", "keyword_requirements", "depth_requirements", "non-overlapping subtopics", "Do not force the exact primary keyword"):
            self.assertIn(text, instruction)
        for field in ("markdown", "claims_used", "sources_used", "verify"):
            self.assertIn(field, _schema_for("full_article"))

    def test_industry_policy_is_dynamic_without_weakening_fixed_evidence_rules(self) -> None:
        from seo_control.application.content_generator import FIXED_CONTENT_SAFETY_RULES, _schema_for  # noqa: E402

        instruction = _stage_instruction("industry_rules")
        for text in ("explicit project industry", "provisional industry", "YMYL", "SaaS", "never weaken fixed_safety_rules"):
            self.assertIn(text, instruction)
        schema = _schema_for("industry_rules")
        for field in ("industry_confidence", "industry_basis", "evidence_policy", "high_risk_claims", "localization_rules"):
            self.assertIn(field, schema)
        self.assertTrue(any("another project" in rule for rule in FIXED_CONTENT_SAFETY_RULES))
        self.assertIn("fixed_safety_rules always have higher priority", SYSTEM_PROMPT)

    def test_all_active_stages_are_explicit_and_avoid_fixed_length_constraints(self) -> None:
        from seo_control.application.content_generator import _schema_for, _stage_instruction  # noqa: E402

        for stage in ("competitor_relevance", "industry_rules", "semantic", "title", "outline", "full_article", "qa"):
            self.assertTrue(_stage_instruction(stage))
            self.assertTrue(_schema_for(stage))
        qa_schema = _schema_for("qa")
        for field in ("status", "scores", "targeted_rewrite", "unresolved_verify"):
            self.assertIn(field, qa_schema)
        self.assertNotIn("word_budget", _schema_for("outline"))
        self.assertNotIn("target length", _stage_instruction("outline").lower())

    def test_reader_markdown_contract_prevents_recurring_table_and_list_defects(self) -> None:
        for stage in ("full_article", "qa", "targeted_rewrite"):
            instruction = _stage_instruction(stage)
            for text in ("never output raw HTML", "Markdown header row", "explicit consecutive markers", "- [ ]"):
                self.assertIn(text, instruction)

    def test_full_article_prompt_enforces_h2_depth_without_word_count_padding(self) -> None:
        from seo_control.application.content_generator import PROMPT_VERSION, _schema_for  # noqa: E402

        self.assertEqual("people_first_full_article_v26", PROMPT_VERSION)
        for stage in ("outline", "full_article", "qa", "targeted_rewrite"):
            instruction = _stage_instruction(stage)
            for text in ("new reader value", "unique information gain", "four to six non-overlapping", "never pad", "one concise statement"):
                self.assertIn(text, instruction)
        outline_schema = _schema_for("outline")
        for field in ("keyword_requirements", "minimum_supporting_terms", "depth_requirements", "minimum_subtopics", "reader_outcome", "practical_detail"):
            self.assertIn(field, outline_schema)
        self.assertIn("h2_reviews", _schema_for("qa"))
        self.assertIn("repetition_control", _schema_for("qa"))

    def test_full_article_prompt_does_not_require_a_reference_footer(self) -> None:
        instruction = _stage_instruction("full_article")
        self.assertNotIn("Authority-reference policy", instruction)
        self.assertNotIn("权威参考与验证链接", instruction)
        self.assertNotIn("authority-reference footer", instruction)
        self.assertIn("Use only supplied evidence for material factual claims", instruction)

    def test_full_article_requires_a_3000_word_article_without_an_appendix(self) -> None:
        instruction = _stage_instruction("full_article")
        for text in ("must exceed 3,000 English words", "not with repetitive recaps"):
            self.assertIn(text, instruction)
        self.assertNotIn("separately appended", instruction)

    def test_editorial_voice_policy_rejects_canned_ai_prose(self) -> None:
        instruction = _stage_instruction("full_article")
        for text in ("Natural editorial-voice policy", "not like an AI assistant", "In today's fast-paced world", "Aim for clarity and credible reader value"):
            self.assertIn(text, instruction)

    def test_full_article_prompt_keeps_a_closed_outline_and_safe_source_free_mode(self) -> None:
        instruction = _stage_instruction("full_article")
        for text in ("approved H2 set is closed", "exactly once", "sources is empty", "electrical values"):
            self.assertIn(text, instruction)

    def test_source_roles_keep_gsc_private_and_competitors_non_evidentiary(self) -> None:
        from seo_control.application.content_generator import _stage_instruction  # noqa: E402

        for stage in ("semantic", "outline", "full_article", "qa", "targeted_rewrite"):
            instruction = _stage_instruction(stage)
            self.assertIn("source_type=gsc_performance", instruction)
            self.assertIn("private search-demand intelligence", instruction)
            self.assertIn("competitor pages, competitor analysis, and learning memories", instruction)


if __name__ == "__main__":
    unittest.main()
