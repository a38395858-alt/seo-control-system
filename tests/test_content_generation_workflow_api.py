"""End-to-end contracts for the evidence-grounded content generation workflow.

The fixtures deliberately replace the provider adapter.  They prove that the
application orchestrates the content-synthesis stages and persists traceable
results without making a real network request or exposing credentials.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.content_generator import PROMPT_VERSION  # noqa: E402
from seo_control.web import KeywordDiscoveryRequestHandler, create_server  # noqa: E402


class FakeContentGenerator:
    """A deterministic, source-aware OpenAI-compatible content fixture."""

    provider = "gemini"
    model = "gemini-content-fixture"

    def generate(self, **request: object) -> str:
        stage = request.get("stage")
        if stage == "industry_rules":
            project_context = request.get("project_context") if isinstance(request.get("project_context"), dict) else {}
            explicit_industry = str(project_context.get("industry") or "")
            is_health = "health" in explicit_industry.lower() or "medical" in explicit_industry.lower()
            return json.dumps(
                {
                    "industry": explicit_industry or "SEO software",
                    "industry_confidence": 1.0 if explicit_industry else 0.86,
                    "industry_basis": "explicit" if explicit_industry else "inferred",
                    "audience_language": "Plain language for US small-business buyers",
                    "tone_rules": ["Be practical and precise"],
                    "structure_rules": ["Explain workflow fit before feature comparison"],
                    "terminology_rules": ["Define specialist metrics on first use"],
                    "evidence_policy": {"risk_level": "ymyl" if is_health else "standard", "preferred_sources": ["Clinical guidelines", "Government health sources"] if is_health else ["Official product documentation"], "high_risk_claims": ["Diagnosis and treatment outcomes"] if is_health else ["Current pricing"], "required_disclosures": ["Not a substitute for professional medical advice"] if is_health else []},
                    "content_patterns": ["Decision checklist"],
                    "prohibited_claims": ["Guaranteed rankings"],
                    "conversion_rules": ["Use a restrained trial CTA"],
                    "localization_rules": ["Use US English"],
                }
            )
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
        if stage == "full_article":
            body = " ".join(["practical"] * 3001)
            return json.dumps(
                {
                    "title": "SEO Tools for Small Businesses: A Practical Guide",
                    "meta_description": "A practical framework for comparing SEO tools.",
                    "markdown": f"# SEO Tools for Small Businesses: A Practical Guide\n\nSEO tools for small business should be chosen by the work you need to complete.\n\n## How to compare SEO tools\n\nCompare the workflow, practical checks, and evidence boundary before choosing.\n\n## Questions to ask before you buy\n\nConfirm fit before committing. {body}",
                    "sources_used": [],
                    "claims_used": [],
                    "verify": ["Current vendor pricing requires first-party confirmation"],
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
            with urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def create_asset(self, *, industry: str = "") -> tuple[int, int]:
        status, project = self.request_json(
            "POST", "/api/projects", {"name": "Content generation", "industry": industry, "country_code": "US", "language_code": "en"}
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
        asset_id = asset["id"]  # type: ignore[index]
        self._mark_competitor_learning_complete(project_id, asset_id)
        return project_id, asset_id

    def _mark_competitor_learning_complete(self, project_id: int, asset_id: int) -> None:
        connection = sqlite3.connect(self.server.database_path)
        try:
            cursor = connection.execute(
                """INSERT INTO competitor_research_runs(
                       project_id,content_asset_id,query,locale,status,discovered_count,usable_count,analysis_json,completed_at
                   ) VALUES(?,?,?,?, 'completed',1,1,?,CURRENT_TIMESTAMP)""",
                (project_id, asset_id, "seo tools for small business", "US/en", json.dumps({"writing_patterns": ["reader decision order"]})),
            )
            run_id = cursor.lastrowid
            cursor = connection.execute(
                """INSERT INTO competitor_content_memory(
                       project_id,normalized_url,url,domain,page_title,content,content_hash,structure_json
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (project_id, "https://competitor.example/seo-tools", "https://competitor.example/seo-tools", "competitor.example", "Competitor guide", "Competitor article for structural-learning fixture.", "fixture-hash", "{}"),
            )
            connection.execute(
                """INSERT INTO competitor_research_items(
                       research_run_id,memory_id,rank,search_title,url,domain,status
                   ) VALUES(?,?,?,?,?,?, 'selected')""",
                (run_id, cursor.lastrowid, 1, "Competitor guide", "https://competitor.example/seo-tools", "competitor.example"),
            )
            connection.commit()
        finally:
            connection.close()

    def test_staged_generation_persists_brief_outline_draft_and_auditable_model_metadata(self) -> None:
        project_id, asset_id = self.create_asset()

        status, brief = self.request_json(
            "POST",
            f"/api/content-assets/{asset_id}/generate-brief",
            {"project_id": project_id, "provider": "gemini", "target_audience": "US small business owners", "business_goal": "commercial", "target_length": 1200, "sources": []},
        )
        self.assertEqual(201, status)
        self.assertEqual("US small business owners", brief["brief"]["target_audience"])  # type: ignore[index]
        self.assertEqual("SEO software", brief["brief"]["brief"]["industry_rules"]["industry"])  # type: ignore[index]

        status, outline = self.request_json(
            "POST", f"/api/content-assets/{asset_id}/generate-outline", {"project_id": project_id, "provider": "gemini"}
        )
        self.assertEqual(201, status)
        self.assertEqual(2, len(outline["outline"]["sections"]))  # type: ignore[index]

        status, draft = self.request_json(
            "POST", f"/api/content-assets/{asset_id}/generate-draft", {"project_id": project_id, "provider": "gemini"}
        )
        self.assertEqual(201, status)
        self.assertNotIn("[VERIFY]", draft["draft"]["markdown"])  # type: ignore[index]
        self.assertEqual("gemini", draft["draft"]["provider"])  # type: ignore[index]

        status, detail = self.request_json("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual(200, status)
        # The content workflow is complete even when factual verification keeps
        # the asset itself out of the publish-ready state.
        self.assertEqual("needs_revision", detail["status"])  # type: ignore[index]
        self.assertEqual("completed", detail["content_status"])  # type: ignore[index]
        self.assertEqual(draft["draft"]["id"], detail["current_draft"]["id"])  # type: ignore[index]
        self.assertEqual(1, len(detail["drafts"]))  # type: ignore[index]
        runs = detail["generation_runs"]  # type: ignore[index]
        self.assertEqual(["industry_rules", "semantic", "title", "outline", "full_article", "qa"], [run["stage"] for run in runs])
        self.assertTrue(all(run["status"] == "completed" for run in runs))
        self.assertTrue(all(run["provider"] == "gemini" for run in runs))
        self.assertTrue(all(run["prompt_version"] == PROMPT_VERSION for run in runs))
        policy_stages = {"semantic", "title", "outline", "full_article"}
        for run in runs:
            if run["stage"] in policy_stages:
                self.assertEqual("SEO software", run["input"]["writing_policy"]["industry_rules"]["industry"])
                self.assertTrue(run["input"]["writing_policy"]["fixed_safety_rules"])
        self.assertEqual("needs_verification", detail["current_draft"]["qa_status"])  # type: ignore[index]
        self.assertNotIn("secret", json.dumps(detail).lower())

    def test_generating_a_draft_automatically_creates_and_binds_h2_images(self) -> None:
        project_id, asset_id = self.create_asset()
        self.server.ai_settings_path.write_text(
            json.dumps({
                "providers": {
                    "openai": {"api_key": "test-key", "base_url": "https://images.test/v1", "model": "text-test"},
                },
                "integrations": {
                    "image_generation": {"provider": "openai", "model": "image-test"},
                },
            }),
            encoding="utf-8",
        )
        self.request_json(
            "POST", f"/api/content-assets/{asset_id}/generate-brief",
            {"project_id": project_id, "provider": "gemini", "target_audience": "US buyers", "business_goal": "commercial", "sources": []},
        )
        self.request_json(
            "POST", f"/api/content-assets/{asset_id}/generate-outline",
            {"project_id": project_id, "provider": "gemini"},
        )

        def create_test_image(*, prompt: str, output_path: Path, model: str) -> bool:
            self.assertTrue(prompt)
            self.assertEqual("image-test", model)
            Image.new("RGB", (1024, 1024), "#dbeafe").save(output_path, format="PNG")
            return True

        test_web_root = Path(self.temp.name) / "web"
        with (
            patch("seo_control.web.WEB_ROOT", test_web_root),
            patch.object(KeywordDiscoveryRequestHandler, "_designer_image_prompt", return_value="A matching section illustration without text."),
            patch.object(KeywordDiscoveryRequestHandler, "_generate_image_with_local_proxy", side_effect=create_test_image),
        ):
            status, generated = self.request_json(
                "POST", f"/api/content-assets/{asset_id}/generate-draft",
                {"project_id": project_id, "provider": "gemini"},
            )

        self.assertEqual(201, status)
        self.assertEqual(2, generated["image_generation"]["total"])  # type: ignore[index]
        self.assertEqual(2, generated["image_generation"]["ready"])  # type: ignore[index]
        self.assertEqual([], generated["image_generation"]["failed"])  # type: ignore[index]
        _status, detail = self.request_json("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual(2, len(detail["section_images"]))  # type: ignore[index]
        self.assertTrue(all(image["status"] == "ready" for image in detail["section_images"]))  # type: ignore[index]
        for image in detail["section_images"]:  # type: ignore[index]
            self.assertTrue((test_web_root / image["image_url"].lstrip("/")).is_file())

    def test_explicit_ymyl_industry_is_authoritative_and_project_scoped(self) -> None:
        health_project_id, health_asset_id = self.create_asset(industry="Healthcare")
        status, health_brief = self.request_json(
            "POST", f"/api/content-assets/{health_asset_id}/generate-brief",
            {"project_id": health_project_id, "provider": "gemini", "target_audience": "US patients", "business_goal": "informational", "sources": []},
        )
        self.assertEqual(201, status)
        health_rules = health_brief["brief"]["brief"]["industry_rules"]  # type: ignore[index]
        self.assertEqual("Healthcare", health_rules["industry"])
        self.assertEqual("explicit", health_rules["industry_basis"])
        self.assertEqual(1.0, health_rules["industry_confidence"])
        self.assertEqual("ymyl", health_rules["evidence_policy"]["risk_level"])

        software_project_id, software_asset_id = self.create_asset(industry="SaaS")
        status, software_brief = self.request_json(
            "POST", f"/api/content-assets/{software_asset_id}/generate-brief",
            {"project_id": software_project_id, "provider": "gemini", "target_audience": "US buyers", "business_goal": "commercial", "sources": []},
        )
        self.assertEqual(201, status)
        software_rules = software_brief["brief"]["brief"]["industry_rules"]  # type: ignore[index]
        self.assertEqual("SaaS", software_rules["industry"])
        self.assertEqual("standard", software_rules["evidence_policy"]["risk_level"])
        self.assertNotIn("Healthcare", json.dumps(software_brief))

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
