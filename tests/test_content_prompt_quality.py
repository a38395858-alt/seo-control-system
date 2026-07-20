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

    def test_all_active_stages_are_explicit_and_avoid_qa_or_length_constraints(self) -> None:
        from seo_control.application.content_generator import _schema_for, _stage_instruction  # noqa: E402

        for stage in ("semantic", "title", "outline", "section", "assembly"):
            self.assertTrue(_stage_instruction(stage))
            self.assertTrue(_schema_for(stage))
        self.assertEqual("{}", _schema_for("qa"))
        self.assertNotIn("word_budget", _schema_for("outline"))
        self.assertNotIn("target length", _stage_instruction("outline").lower())


if __name__ == "__main__":
    unittest.main()
