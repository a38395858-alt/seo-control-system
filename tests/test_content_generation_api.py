"""Red contract tests for the staged, versioned content-generation workflow."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.web import create_server  # noqa: E402


class FakeContentGenerator:
    """A deterministic provider double that proves every synthesis stage is called."""

    provider = "openai"
    model = "fake-content-model"

    def __init__(self) -> None:
        self.stages: list[str] = []
        self.stage_inputs: dict[str, list[dict]] = {}

    def run_stage(self, *, stage: str, data: dict) -> dict:
        self.stages.append(stage)
        self.stage_inputs.setdefault(stage, []).append(data)
        if stage == "semantic":
            return {"intent": {"dominant": "commercial", "secondary": [], "reader_job": "compare tools"}, "audience_context": "US buyers", "entities": [], "questions": ["Which tool fits?"], "facts": [], "gaps_or_conflicts": [{"item": "No supplied sources", "action": "verify"}], "angle": "decision guide", "must_cover": ["comparison criteria"], "must_avoid": ["unsupported claims"]}
        if stage == "title":
            return {"candidates": [], "selected_title": data["title_snapshot"], "slug": "seo-tools-guide", "meta_description": "A practical comparison guide.", "selection_reason": "Keeps the approved title."}
        if stage == "outline":
            return {"intro_brief": "Answer the buyer question.", "sections": [{"id": "s1", "heading": "How to compare options", "level": "h2", "reader_question": "What should I compare?", "purpose": "Give criteria", "key_points": ["Start with needs"], "source_ids": [], "evidence_gaps": ["[VERIFY]"], "word_budget": 300, "format": "table"}], "conclusion_brief": "Summarize next steps.", "cta_placement": "after conclusion", "estimated_total_words": 300}
        if stage == "section":
            return {"section_id": data["section"]["id"], "markdown": "## How to compare options\n\nStart with your needs. [VERIFY]", "claims_used": [], "verify": ["No sources supplied"]}
        if stage == "assembly":
            return {"title": data["metadata"]["selected_title"], "meta_description": data["metadata"]["meta_description"], "markdown": "# SEO Tools Guide\n\n## How to compare options\n\nStart with your needs. [VERIFY]", "sources_used": [], "verify": ["No sources supplied"]}
        if stage == "qa":
            return {"status": "needs_verification", "checks": [{"name": "factual support", "status": "verify", "note": "No sources supplied"}], "final_markdown": data["article"]["markdown"], "unresolved_verify": ["No sources supplied"]}
        raise AssertionError(stage)


class FailingAssemblyContentGenerator(FakeContentGenerator):
    def run_stage(self, *, stage: str, data: dict) -> dict:
        if stage == "assembly":
            self.stages.append(stage)
            raise RuntimeError("selected provider assembly timeout")
        return super().run_stage(stage=stage, data=data)


class ContentGenerationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.generator = FakeContentGenerator()
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temp.name) / "content.sqlite3", content_generator=self.generator)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        request = Request(self.base_url + path, data=None if payload is None else json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except Exception as error:
            return error.code, json.loads(error.read())  # type: ignore[attr-defined]

    def asset(self, *, title_text: str = "SEO Tools for Small Businesses: A Practical Guide") -> tuple[int, int]:
        _, project = self.request("POST", "/api/projects", {"name": "Content generation", "country_code": "US", "language_code": "en"})
        project_id = project["id"]  # type: ignore[index]
        self.request("POST", "/api/expanded-keywords", {"project_id": project_id, "seed_keyword": "seo tools", "country_code": "US", "language_code": "en", "keywords": [{"keyword": "seo tools for small business", "is_seo_content_fit": True, "same_topic_as_seed": True}]})
        _, keywords = self.request("GET", f"/api/keywords?project_id={project_id}")
        _, title = self.request("POST", "/api/title-candidates", {"project_id": project_id, "keyword_id": keywords[0]["id"], "title": title_text})  # type: ignore[index]
        self.request("POST", f"/api/title-candidates/{title['id']}/select", {"project_id": project_id})  # type: ignore[index]
        _, asset = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title["id"]})  # type: ignore[index]
        return project_id, asset["id"]  # type: ignore[index]

    def test_generate_runs_skill_stages_and_saves_versioned_draft(self) -> None:
        project_id, asset_id = self.asset()
        status, generated = self.request("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "target_audience": "US small business owners", "business_goal": "commercial", "target_length": 900, "sources": [], "cta": "Compare your shortlist."})

        self.assertEqual(201, status)
        self.assertEqual(["semantic", "title", "outline", "section", "assembly"], self.generator.stages)
        self.assertEqual(1, generated["draft"]["version"])  # type: ignore[index]
        self.assertIn("[VERIFY]", generated["draft"]["markdown"])  # type: ignore[index]
        self.assertEqual("not_run", generated["draft"]["qa_status"])  # type: ignore[index]
        self.assertEqual(5, len(generated["runs"]))  # type: ignore[arg-type]
        self.assertNotIn("target_length", self.generator.stage_inputs["outline"][0])
        drafted_section = self.generator.stage_inputs["section"][0]["section"]
        self.assertEqual(["Start with needs"], drafted_section["key_points"])
        self.assertEqual(["[VERIFY]"], drafted_section["evidence_gaps"])
        self.assertEqual("table", drafted_section["format"])
        self.assertEqual("content_seo_eeat_v2", generated["runs"][0]["prompt_version"])  # type: ignore[index]

        status, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual(200, status)
        self.assertEqual(1, len(detail["drafts"]))  # type: ignore[index]
        self.assertEqual("completed", detail["runs"][-1]["status"])  # type: ignore[index]

    def test_each_generation_creates_a_new_version_without_overwriting_history(self) -> None:
        project_id, asset_id = self.asset()
        request = {"project_id": project_id, "target_audience": "US small business owners", "business_goal": "commercial", "target_length": 900, "sources": []}
        self.assertEqual(201, self.request("POST", f"/api/content-assets/{asset_id}/generate", request)[0])
        self.assertEqual(201, self.request("POST", f"/api/content-assets/{asset_id}/generate", request)[0])
        _, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual([1, 2], [draft["version"] for draft in detail["drafts"]])  # type: ignore[index]

    def test_selected_provider_is_locked_for_every_stage_and_saved_as_one_generation_job(self) -> None:
        project_id, asset_id = self.asset()
        status, generated = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/generate",
            {"project_id": project_id, "provider": "openai", "target_audience": "US buyers", "business_goal": "commercial", "sources": []},
        )

        self.assertEqual(201, status)
        job = generated["generation_job"]  # type: ignore[index]
        self.assertEqual("openai", job["provider"])
        self.assertEqual("fake-content-model", job["model"])
        self.assertEqual("completed", job["status"])
        self.assertTrue(all(run["provider"] == "openai" and run["generation_job_id"] == job["id"] for run in generated["runs"]))  # type: ignore[index]
        self.assertEqual("openai", generated["draft"]["provider"])  # type: ignore[index]

    def test_selected_provider_failure_never_falls_back_or_overwrites_a_previous_draft(self) -> None:
        project_id, asset_id = self.asset()
        success = {"project_id": project_id, "provider": "gemini", "target_audience": "US buyers", "business_goal": "commercial", "sources": []}
        self.assertEqual(201, self.request("POST", f"/api/content-assets/{asset_id}/generate", success)[0])
        self.server.content_generator = FailingAssemblyContentGenerator()

        status, failed = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/generate",
            {"project_id": project_id, "provider": "openai", "target_audience": "US buyers", "business_goal": "commercial", "sources": []},
        )

        self.assertEqual(502, status)
        self.assertIn("ChatGPT", failed["error"])  # type: ignore[index]
        self.assertIn("assembly", failed["error"].lower())  # type: ignore[index]
        self.assertEqual("openai", failed["generation_job"]["provider"])  # type: ignore[index]
        self.assertEqual("failed", failed["generation_job"]["status"])  # type: ignore[index]
        self.assertEqual("assembly", failed["generation_job"]["failed_stage"])  # type: ignore[index]

        _, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual("gemini", detail["current_draft"]["provider"])  # type: ignore[index]
        failed_job = detail["generation_jobs"][-1]  # type: ignore[index]
        failed_runs = [run for run in detail["generation_runs"] if run["generation_job_id"] == failed_job["id"]]  # type: ignore[index]
        self.assertTrue(failed_runs)
        self.assertTrue(all(run["provider"] == "openai" for run in failed_runs))

    def test_one_click_generation_keeps_an_existing_brief_then_generates_outline_and_full_draft(self) -> None:
        project_id, asset_id = self.asset()
        status, brief = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/briefs",
            {"project_id": project_id, "target_audience": "US buyers", "business_goal": "commercial", "sources": ["First-party notes"]},
        )
        self.assertEqual(201, status)
        self.generator.stages.clear()

        status, generated = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/generate",
            {"project_id": project_id, "provider": "openai", "target_audience": "ignored because brief exists", "business_goal": "commercial", "sources": []},
        )

        self.assertEqual(201, status)
        self.assertEqual(["title", "outline", "section", "assembly"], self.generator.stages)
        self.assertNotIn("brief", generated)  # type: ignore[operator]
        _, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual(brief["id"], detail["brief"]["id"])  # type: ignore[index]
        self.assertIsNotNone(detail["current_draft"])  # type: ignore[index]

    def test_full_generation_stops_after_assembly_without_calling_a_qa_model_stage(self) -> None:
        project_id, asset_id = self.asset(title_text="Best SEO Tools for Small Businesses: Pricing and Features Compared")
        status, generated = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/generate",
            {"project_id": project_id, "provider": "openai", "target_audience": "US buyers", "business_goal": "commercial", "sources": []},
        )

        self.assertEqual(201, status)
        self.assertNotIn("qa", self.generator.stages)
        self.assertEqual("not_run", generated["draft"]["qa_status"])  # type: ignore[index]
        self.assertEqual("openai", generated["draft"]["provider"])  # type: ignore[index]
