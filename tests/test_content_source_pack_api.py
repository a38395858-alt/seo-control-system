"""Red contract for structured, evidence-traceable content source packs.

The content-synthesis workflow must retain enough metadata to tell a usable
source from a missing one and to pass the exact same source IDs into every AI
stage.  Free-form pasted material remains supported as a legacy input, but is
normalised before it becomes the article's source pack.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.web import create_server  # noqa: E402


class CapturingSemanticGenerator:
    provider = "openai"
    model = "source-pack-fixture"

    def __init__(self) -> None:
        self.semantic_sources: list[object] | None = None
        self.stage_sources: dict[str, list[object]] = {}

    def generate(self, **request: object) -> dict[str, object]:
        stage = request.get("stage")
        if stage == "semantic":
            self.semantic_sources = request["sources"]  # type: ignore[assignment]
            self.stage_sources["semantic"] = request["sources"]  # type: ignore[assignment]
            return {
                "intent": {"dominant": "informational", "secondary": [], "reader_job": "evaluate tools"},
                "audience_context": "US small business owners",
                "entities": [], "questions": [], "facts": [],
                "gaps_or_conflicts": [{"item": "unavailable source", "action": "verify"}],
                "angle": "evidence-led comparison", "must_cover": [], "must_avoid": [],
            }
        if stage == "title":
            return {"candidates": [], "selected_title": request["title_snapshot"], "slug": "seo-tools", "meta_description": "Evidence-led guide.", "selection_reason": "Approved title."}
        if stage == "outline":
            self.stage_sources["outline"] = request["sources"]  # type: ignore[assignment]
            return {"intro_brief": "Answer first.", "sections": [{"id": "s1", "heading": "Evaluation criteria", "level": "h2", "reader_question": "What matters?", "purpose": "Explain a decision.", "key_points": [], "source_ids": ["official-pricing"], "evidence_gaps": [], "word_budget": 300, "format": "paragraphs"}], "conclusion_brief": "Next steps.", "cta_placement": "end", "estimated_total_words": 300}
        if stage == "chapter_plan":
            return {"section_id": "s1", "writing_goal": "Explain the criteria.", "subtopics": [{"reader_question": "What matters?", "points": ["Use the available source"], "source_ids": ["official-pricing"]}], "must_include": [], "must_avoid_repeating": [], "format": "paragraphs"}
        if stage == "section":
            self.stage_sources["section"] = request["sources"]  # type: ignore[assignment]
            return {"section_id": "s1", "markdown": "## Evaluation criteria\n\n[VERIFY]", "claims_used": [], "verify": ["[VERIFY]"]}
        if stage == "assembly":
            return {"title": "SEO Tools for Small Businesses: A Practical Guide", "meta_description": "Evidence-led guide.", "markdown": "# SEO Tools\n\n[VERIFY]", "sources_used": ["official-pricing"], "verify": ["[VERIFY]"]}
        if stage == "qa":
            return {"status": "needs_verification", "checks": [], "final_markdown": "# SEO Tools\n\n[VERIFY]", "unresolved_verify": ["[VERIFY]"]}
        raise AssertionError(f"Unexpected stage {stage!r}")

    @staticmethod
    def assert_stage(request: dict[str, object], expected: str) -> None:
        if request.get("stage") != expected:
            raise AssertionError(f"Expected {expected}, got {request.get('stage')}")


class ContentSourcePackApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.generator = CapturingSemanticGenerator()
        root = Path(self.temp.name)
        self.server = create_server(
            "127.0.0.1", 0, database_path=root / "source-pack.sqlite3", ai_settings_path=root / "ai-settings.json", content_generator=self.generator
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        request = Request(
            self.base_url + path,
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers={} if payload is None else {"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def content_asset(self) -> tuple[int, int]:
        status, project = self.request("POST", "/api/projects", {"name": "Structured source pack", "country_code": "US", "language_code": "en"})
        self.assertEqual(201, status)
        project_id = project["id"]  # type: ignore[index]
        self.request("POST", "/api/expanded-keywords", {"project_id": project_id, "seed_keyword": "seo tools", "country_code": "US", "language_code": "en", "keywords": [{"keyword": "seo tools for small business", "is_seo_content_fit": True, "same_topic_as_seed": True}]})
        _, keywords = self.request("GET", f"/api/keywords?project_id={project_id}")
        _, title = self.request("POST", "/api/title-candidates", {"project_id": project_id, "keyword_id": keywords[0]["id"], "title": "SEO Tools for Small Businesses: A Practical Guide"})  # type: ignore[index]
        self.request("POST", f"/api/title-candidates/{title['id']}/select", {"project_id": project_id})  # type: ignore[index]
        status, asset = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title["id"]})  # type: ignore[index]
        self.assertEqual(201, status)
        return project_id, asset["id"]  # type: ignore[index]

    def test_structured_sources_are_normalized_persisted_and_sent_to_semantic_analysis(self) -> None:
        project_id, asset_id = self.content_asset()
        source_pack = [
            {
                "source_id": "official-pricing",
                "source_type": "url",
                "url": "https://example.com/pricing",
                "publisher": "Example Inc.",
                "published_at": "2026-01-15",
                "content": "Official product pricing and plan details.",
                "availability": "available",
            },
            {
                "source_id": "unavailable-study",
                "source_type": "url",
                "url": "https://research.example/missing",
                "publisher": "Research Lab",
                "availability": "unavailable",
            },
        ]

        status, generated = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/generate",
            {"project_id": project_id, "target_audience": "US buyers", "business_goal": "commercial", "target_length": 1200, "sources": source_pack},
        )
        self.assertEqual(201, status)
        persisted = generated["brief"]["sources"]  # type: ignore[index]
        self.assertEqual(source_pack, persisted)
        self.assertEqual(source_pack, self.generator.semantic_sources)
        self.assertEqual(source_pack, self.generator.stage_sources["outline"])
        self.assertEqual([source_pack[0]], self.generator.stage_sources["section"])

        status, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual(200, status)
        self.assertEqual("official-pricing", detail["brief"]["sources"][0]["source_id"])  # type: ignore[index]
        self.assertEqual("unavailable", detail["brief"]["sources"][1]["availability"])  # type: ignore[index]

    def test_legacy_pasted_string_is_converted_to_a_traceable_note_source(self) -> None:
        project_id, asset_id = self.content_asset()
        status, generated = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/generate-brief",
            {"project_id": project_id, "target_audience": "US buyers", "business_goal": "informational", "target_length": 1200, "sources": ["Internal product note: verify availability before publication."]},
        )
        self.assertEqual(201, status)
        source = generated["brief"]["sources"][0]  # type: ignore[index]
        self.assertEqual("note", source["source_type"])  # type: ignore[index]
        self.assertEqual("available", source["availability"])  # type: ignore[index]
        self.assertTrue(source["source_id"].startswith("source-"))  # type: ignore[index]
        self.assertIn("Internal product note", source["content"])  # type: ignore[index]

    def test_previous_frontend_provided_status_is_normalized_for_compatibility(self) -> None:
        project_id, asset_id = self.content_asset()
        status, generated = self.request(
            "POST", f"/api/content-assets/{asset_id}/generate-brief",
            {"project_id": project_id, "sources": [{"source_id": "pasted-note", "source_type": "note", "content": "A pasted company note.", "availability": "provided"}]},
        )
        self.assertEqual(201, status)
        self.assertEqual("available", generated["brief"]["sources"][0]["availability"])  # type: ignore[index]

    def test_malformed_structured_source_is_rejected_before_creating_a_brief_or_ai_run(self) -> None:
        project_id, asset_id = self.content_asset()
        status, payload = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/generate-brief",
            {"project_id": project_id, "sources": [{"source_id": "missing-url", "source_type": "url", "availability": "available"}]},
        )
        self.assertEqual(400, status)
        self.assertIn("source", payload["error"].lower())  # type: ignore[index]
        _status, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertIsNone(detail["brief"])  # type: ignore[index]
        self.assertEqual([], detail["generation_runs"])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
