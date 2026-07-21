"""End-to-end contracts for the evidence-grounded content generation workflow.

The fixtures deliberately replace the provider adapter.  They prove that the
application orchestrates the content-synthesis stages and persists traceable
results without making a real network request or exposing credentials.
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


class FakeContentGenerator:
    """A deterministic, source-aware OpenAI-compatible content fixture."""

    provider = "gemini"
    model = "gemini-content-fixture"

    def generate(self, **request: object) -> str:
        stage = request.get("stage")
        if stage == "semantic":
            return json.dumps(
                {
                    "intent": {"dominant": "commercial", "secondary": [], "reader_job": "compare options"},
                    "audience_context": "US small business owners",
                    "entities": [],
                    "questions": ["Which SEO tool fits my workflow?"],
                    "facts": [],
                    "gaps_or_conflicts": [{"item": "Vendor pricing", "action": "verify"}],
                    "angle": "A practical buying decision framework.",
                    "must_cover": ["selection criteria"],
                    "must_avoid": ["unsupported rankings"],
                }
            )
        if stage == "title":
            return json.dumps({"candidates": [], "selected_title": "SEO Tools for Small Businesses: A Practical Guide", "slug": "seo-tools", "meta_description": "A practical framework for comparing SEO tools.", "selection_reason": "Keeps the approved title."})
        if stage == "outline":
            return json.dumps(
                {
                    "intro_brief": "Answer the buyer question first.",
                    "sections": [
                        {
                            "heading": "How to compare SEO tools",
                            "purpose": "Give buyers clear selection criteria.",
                            "word_budget": 450,
                            "level": "h2",
                            "source_ids": [],
                            "evidence_gaps": ["[VERIFY] current vendor pricing"],
                        },
                        {
                            "heading": "Questions to ask before you buy",
                            "purpose": "Help readers avoid a poor fit.",
                            "word_budget": 350,
                            "level": "h2",
                            "source_ids": [],
                            "evidence_gaps": [],
                        },
                    ],
                    "estimated_total_words": 1200,
                }
            )
        if stage == "chapter_plan":
            return json.dumps({"section_id": request["current_section"]["id"], "writing_goal": "Develop this buyer decision.", "subtopics": [{"reader_question": request["current_section"].get("reader_question", ""), "points": request["current_section"].get("key_points", []), "source_ids": request["current_section"].get("source_ids", [])}], "must_include": [], "must_avoid_repeating": [], "format": request["current_section"].get("format", "paragraphs")})
        if stage == "section":
            return json.dumps({"section_id": "s1", "markdown": "## Practical comparison\n\n[VERIFY] current vendor pricing", "claims_used": [], "verify": ["[VERIFY] current vendor pricing"]})
        if stage == "assembly":
            return json.dumps(
                {
                    "title": "SEO Tools for Small Businesses: A Practical Guide",
                    "meta_description": "A practical framework for comparing SEO tools.",
                    "markdown": (
                        "# SEO Tools for Small Businesses: A Practical Guide\n\n"
                        "Choose tools by the work you need to complete, not by unsupported rankings.\n\n"
                        "## How to compare SEO tools\n\n"
                        "[VERIFY] Confirm current vendor pricing from first-party sources before publishing."
                    ),
                    "sources_used": [],
                    "verify": ["[VERIFY] current vendor pricing"],
                }
            )
        if stage == "qa":
            return json.dumps({"status": "needs_verification", "checks": [{"name": "factual support", "status": "verify", "note": "No first-party pricing source supplied."}], "final_markdown": "# SEO Tools for Small Businesses: A Practical Guide\n\n[VERIFY] current vendor pricing", "unresolved_verify": ["[VERIFY] current vendor pricing"]})
        raise AssertionError(f"Unexpected content generation stage: {stage!r}")


class InvalidContentGenerator:
    def generate(self, **_request: object) -> str:
        return "this is not valid JSON"


class ContentGenerationWorkflowApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.server = create_server(
            "127.0.0.1",
            0,
            database_path=root / "content-generation.sqlite3",
            ai_settings_path=root / "ai-settings.json",
        )
        self.server.content_generator = FakeContentGenerator()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request_json(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
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

    def create_asset(self) -> tuple[int, int]:
        status, project = self.request_json(
            "POST", "/api/projects", {"name": "Content generation", "country_code": "US", "language_code": "en"}
        )
        self.assertEqual(201, status)
        project_id = project["id"]  # type: ignore[index]
        status, _saved = self.request_json(
            "POST",
            "/api/expanded-keywords",
            {
                "project_id": project_id,
                "country_code": "US",
                "language_code": "en",
                "seed_keyword": "seo tools",
                "keywords": [{"keyword": "seo tools for small business", "is_seo_content_fit": True, "same_topic_as_seed": True}],
            },
        )
        self.assertEqual(201, status)
        _status, keywords = self.request_json("GET", f"/api/keywords?project_id={project_id}")
        keyword_id = keywords[0]["id"]  # type: ignore[index]
        status, title = self.request_json(
            "POST",
            "/api/title-candidates",
            {"project_id": project_id, "keyword_id": keyword_id, "title": "SEO Tools for Small Businesses: A Practical Guide"},
        )
        self.assertEqual(201, status)
        title_id = title["id"]  # type: ignore[index]
        status, _selected = self.request_json("POST", f"/api/title-candidates/{title_id}/select", {"project_id": project_id})
        self.assertEqual(200, status)
        status, asset = self.request_json(
            "POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title_id, "content_type": "guide"}
        )
        self.assertEqual(201, status)
        return project_id, asset["id"]  # type: ignore[index]

    def test_staged_generation_persists_brief_outline_draft_and_auditable_model_metadata(self) -> None:
        project_id, asset_id = self.create_asset()

        status, brief = self.request_json(
            "POST",
            f"/api/content-assets/{asset_id}/generate-brief",
            {"project_id": project_id, "provider": "gemini", "target_audience": "US small business owners", "business_goal": "commercial", "target_length": 1200, "sources": []},
        )
        self.assertEqual(201, status)
        self.assertEqual("US small business owners", brief["brief"]["target_audience"])  # type: ignore[index]

        status, outline = self.request_json(
            "POST", f"/api/content-assets/{asset_id}/generate-outline", {"project_id": project_id, "provider": "gemini"}
        )
        self.assertEqual(201, status)
        self.assertEqual(2, len(outline["outline"]["sections"]))  # type: ignore[index]

        status, draft = self.request_json(
            "POST", f"/api/content-assets/{asset_id}/generate-draft", {"project_id": project_id, "provider": "gemini"}
        )
        self.assertEqual(201, status)
        self.assertIn("[VERIFY]", draft["draft"]["markdown"])  # type: ignore[index]
        self.assertEqual("gemini", draft["draft"]["provider"])  # type: ignore[index]

        status, detail = self.request_json("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual(200, status)
        self.assertEqual("in_review", detail["status"])  # type: ignore[index]
        self.assertEqual(draft["draft"]["id"], detail["current_draft"]["id"])  # type: ignore[index]
        self.assertEqual(1, len(detail["drafts"]))  # type: ignore[index]
        runs = detail["generation_runs"]  # type: ignore[index]
        self.assertEqual(["semantic", "title", "outline", "chapter_plan", "section", "chapter_plan", "section", "assembly"], [run["stage"] for run in runs])
        self.assertTrue(all(run["status"] == "completed" for run in runs))
        self.assertTrue(all(run["provider"] == "gemini" for run in runs))
        self.assertTrue(all(run["prompt_version"] == "content_competitor_learning_v11" for run in runs))
        self.assertEqual("not_run", detail["current_draft"]["qa_status"])  # type: ignore[index]
        self.assertNotIn("secret", json.dumps(detail).lower())

    def test_generation_failure_is_logged_without_overwriting_the_existing_draft(self) -> None:
        project_id, asset_id = self.create_asset()
        self.request_json("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "provider": "openai", "target_audience": "US readers", "business_goal": "commercial", "target_length": 1200, "sources": []})
        _status, before = self.request_json("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        before_markdown = before["current_draft"]["markdown"]  # type: ignore[index]

        self.server.content_generator = InvalidContentGenerator()
        status, failed = self.request_json(
            "POST", f"/api/content-assets/{asset_id}/generate-draft", {"project_id": project_id, "provider": "deepseek"}
        )
        self.assertEqual(502, status)
        self.assertIn("content", failed["error"].lower())  # type: ignore[index]

        _status, detail = self.request_json("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual(before_markdown, detail["current_draft"]["markdown"])  # type: ignore[index]
        self.assertEqual("failed", detail["generation_runs"][-1]["status"])  # type: ignore[index]
        self.assertEqual("deepseek", detail["generation_runs"][-1]["provider"])  # type: ignore[index]

    def test_unconfigured_content_provider_returns_a_clear_503_without_creating_a_draft(self) -> None:
        project_id, asset_id = self.create_asset()
        self.server.content_generator = None
        self.server.ai_settings_path = Path(self.temp.name) / "unconfigured-ai-settings.json"
        status, payload = self.request_json(
            "POST", f"/api/content-assets/{asset_id}/generate-brief", {"project_id": project_id, "provider": "openai"}
        )
        self.assertEqual(503, status)
        self.assertIn("config", payload["error"].lower())  # type: ignore[index]
        _status, detail = self.request_json("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertIsNone(detail["current_draft"])  # type: ignore[index]
        self.assertEqual([], detail["drafts"])  # type: ignore[index]

    def test_content_generation_has_its_own_provider_assignment_without_exposing_keys(self) -> None:
        status, settings = self.request_json(
            "POST",
            "/api/settings/ai",
            {
                "providers": {
                    "openai": {"api_key": "openai-secret", "base_url": "https://openai.example/v1", "model": "gpt-test"},
                    "gemini": {"api_key": "gemini-secret", "base_url": "https://gemini.example/v1", "model": "gemini-test"},
                    "deepseek": {"api_key": "deepseek-secret", "base_url": "https://deepseek.example/v1", "model": "deepseek-test"},
                },
                "assignments": {"keyword_review": "openai", "title_generation": "gemini", "content_generation": "deepseek"},
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("deepseek", settings["assignments"]["content_generation"])  # type: ignore[index]
        self.assertEqual("deepseek-test", settings["providers"]["deepseek"]["model"])  # type: ignore[index]
        self.assertNotIn("-secret", json.dumps(settings))


if __name__ == "__main__":
    unittest.main()
