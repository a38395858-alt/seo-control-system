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
        for text in ("title promise", "source IDs", "decision order", "Source column"):
            self.assertIn(text, instruction)

    def test_competitor_evidence_is_internal_and_step_titles_require_a_checklist(self) -> None:
        self.assertIn("internal evidence keys", SYSTEM_PROMPT)
        self.assertIn("Never print an internal source ID", SYSTEM_PROMPT)
        self.assertIn("ordered verification section", _stage_instruction("outline"))
        self.assertIn("Never print source IDs", _stage_instruction("section"))
        self.assertIn("strictly forbidden in all reader-facing text", _stage_instruction("assembly"))

    def test_prompt_requires_multi_article_depth_without_a_fixed_length_or_h2_count(self) -> None:
        self.assertIn("deep original synthesis of multiple supplied articles", SYSTEM_PROMPT)
        self.assertIn("not a short summary", _stage_instruction("competitor_analysis"))
        self.assertIn("one substantial chapter", _stage_instruction("section"))
        self.assertIn("must be preserved locally", _stage_instruction("assembly"))

    def test_each_h2_receives_a_detailed_chapter_plan_before_prose(self) -> None:
        from seo_control.application.content_generator import _schema_for  # noqa: E402

        self.assertIn("exactly one approved H2", _stage_instruction("chapter_plan"))
        self.assertIn("chapter_plan is binding", _stage_instruction("section"))
        self.assertIn("subtopics", _schema_for("chapter_plan"))

    def test_all_active_stages_are_explicit_and_avoid_qa_or_length_constraints(self) -> None:
        from seo_control.application.content_generator import _schema_for, _stage_instruction  # noqa: E402

        for stage in ("competitor_relevance", "semantic", "title", "outline", "chapter_plan", "section", "assembly"):
            self.assertTrue(_stage_instruction(stage))
            self.assertTrue(_schema_for(stage))
        self.assertEqual("{}", _schema_for("qa"))
        self.assertNotIn("word_budget", _schema_for("outline"))
        self.assertNotIn("target length", _stage_instruction("outline").lower())


if __name__ == "__main__":
    unittest.main()
