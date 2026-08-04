"""Red contract tests for the staged, versioned content-generation workflow."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.web import competitor_candidate_exclusion_reason, create_server  # noqa: E402


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
        if stage == "industry_rules":
            return {"industry": "SEO software", "industry_confidence": 0.9, "industry_basis": "inferred", "audience_language": "US English", "tone_rules": ["Practical"], "structure_rules": ["Decision-led"], "terminology_rules": [], "evidence_policy": {"risk_level": "standard", "preferred_sources": ["Official documentation"], "high_risk_claims": ["Pricing"], "required_disclosures": []}, "content_patterns": [], "prohibited_claims": ["Guaranteed rankings"], "conversion_rules": [], "localization_rules": []}
        if stage == "semantic":
            return {"intent": {"dominant": "commercial", "secondary": [], "reader_job": "compare tools"}, "audience_context": "US buyers", "entities": [], "questions": ["Which tool fits?"], "facts": [], "gaps_or_conflicts": [{"item": "No supplied sources", "action": "verify"}], "angle": "decision guide", "must_cover": ["comparison criteria"], "must_avoid": ["unsupported claims"]}
        if stage == "title":
            return {"candidates": [], "selected_title": data["title_snapshot"], "slug": "seo-tools-guide", "meta_description": "A practical comparison guide.", "selection_reason": "Keeps the approved title."}
        if stage == "outline":
            return {"intro_brief": "Answer the buyer question.", "sections": [{"id": "s1", "heading": "How to compare options", "level": "h2", "reader_question": "What should I compare?", "purpose": "Give criteria", "key_points": ["Start with needs"], "source_ids": [], "evidence_gaps": ["[VERIFY]"], "word_budget": 300, "format": "table"}], "conclusion_brief": "Summarize next steps.", "cta_placement": "after conclusion", "estimated_total_words": 300}
        if stage == "chapter_plan":
            return {"section_id": data["current_section"]["id"], "writing_goal": "Explain the current decision in depth.", "subtopics": [{"reader_question": "What should I compare first?", "points": ["Start with needs", "Verify evidence"], "source_ids": data["current_section"].get("source_ids", [])}], "must_include": ["A practical decision check"], "must_avoid_repeating": ["the introduction"], "format": data["current_section"].get("format", "paragraphs")}
        if stage == "section":
            return {"section_id": data["section"]["id"], "markdown": "## How to compare options\n\nStart with your needs. [VERIFY]", "claims_used": [], "verify": ["No sources supplied"]}
        if stage == "assembly":
            return {"title": data["metadata"]["selected_title"], "meta_description": data["metadata"]["meta_description"], "intro_markdown": "Choose based on your workflow before comparing options.", "conclusion_markdown": "Use the checklist to confirm the right fit.", "sources_used": [], "verify": ["No sources supplied"]}
        if stage == "content_tags":
            return {"tags": ["SEO Tools", "Product Comparison", "Buying Guide"]}
        if stage == "qa":
            return {"status": "needs_verification", "checks": [{"name": "factual support", "status": "verify", "note": "No sources supplied"}], "targeted_rewrite": [{"target": "How to compare options", "issue": "Add one practical check", "instruction": "Add one concise verification check without changing other sections."}], "final_markdown": data["article"]["markdown"], "unresolved_verify": ["No sources supplied"]}
        if stage == "targeted_rewrite":
            return {"markdown": data["article"]["markdown"] + "\n\nPractical verification: confirm the evidence before deciding.", "meta_description": data["article"]["meta_description"], "applied_targets": [item["target"] for item in data["instructions"]], "verify": ["No sources supplied"]}
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
        self.assertEqual(["industry_rules", "semantic", "title", "outline", "chapter_plan", "section", "assembly", "content_tags"], self.generator.stages)
        self.assertEqual(1, generated["draft"]["version"])  # type: ignore[index]
        self.assertIn("[VERIFY]", generated["draft"]["markdown"])  # type: ignore[index]
        self.assertEqual("not_run", generated["draft"]["qa_status"])  # type: ignore[index]
        self.assertEqual(7, len(generated["runs"]))  # type: ignore[arg-type]
        self.assertNotIn("target_length", self.generator.stage_inputs["outline"][0])
        drafted_section = self.generator.stage_inputs["section"][0]["section"]
        self.assertEqual(["Start with needs"], drafted_section["key_points"])
        self.assertEqual(["[VERIFY]"], drafted_section["evidence_gaps"])
        self.assertEqual("table", drafted_section["format"])
        self.assertEqual("Explain the current decision in depth.", drafted_section["chapter_plan"]["writing_goal"])
        self.assertNotIn("section_drafts", self.generator.stage_inputs["assembly"][0])
        self.assertIn("## How to compare options", generated["draft"]["markdown"])  # type: ignore[index]
        self.assertEqual("people_first_evidence_routed_v20", generated["runs"][0]["prompt_version"])  # type: ignore[index]

        status, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual(200, status)
        self.assertEqual(1, len(detail["drafts"]))  # type: ignore[index]
        self.assertEqual(["SEO Tools", "Product Comparison", "Buying Guide"], detail["tags"])  # type: ignore[index]
        self.assertEqual("completed", detail["runs"][-1]["status"])  # type: ignore[index]

    def test_robots_blocked_competitor_url_is_preserved_without_body(self) -> None:
        project_id, _asset_id = self.asset()
        connection = sqlite3.connect(self.server.database_path)
        try:
            self.server.RequestHandlerClass._archive_robots_blocked_url(  # type: ignore[attr-defined]
                connection,
                project_id,
                {
                    "url": "https://example.com/competitor-guide",
                    "domain": "example.com",
                    "title": "Competitor guide",
                    "rank": 4,
                    "search_query": "outdoor stair lighting",
                },
                RuntimeError("robots.txt disallows automated content extraction"),
            )
        finally:
            connection.close()
        status, archive = self.request("GET", f"/api/competitor-url-archive?project_id={project_id}")
        self.assertEqual(200, status)
        self.assertEqual(1, len(archive))  # type: ignore[arg-type]
        entry = archive[0]  # type: ignore[index]
        self.assertEqual("robots_blocked", entry["status"])
        self.assertEqual("https://example.com/competitor-guide", entry["url"])
        self.assertEqual("outdoor stair lighting", entry["last_query"])

    def test_major_social_platforms_are_skipped_before_competitor_crawling(self) -> None:
        for domain in ("reddit.com", "pinterest.com", "linkedin.com", "x.com", "quora.com"):
            reason = competitor_candidate_exclusion_reason(
                {"domain": domain, "url": f"https://www.{domain}/example", "title": "Community post"},
                "ledsteplight.com",
            )
            self.assertIsNotNone(reason, domain)
            self.assertIn("excluded", reason or "")

    def test_each_generation_creates_a_new_version_without_overwriting_history(self) -> None:
        project_id, asset_id = self.asset()
        request = {"project_id": project_id, "target_audience": "US small business owners", "business_goal": "commercial", "target_length": 900, "sources": []}
        self.assertEqual(201, self.request("POST", f"/api/content-assets/{asset_id}/generate", request)[0])
        self.assertEqual(201, self.request("POST", f"/api/content-assets/{asset_id}/generate", request)[0])
        _, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual([1, 2], [draft["version"] for draft in detail["drafts"]])  # type: ignore[index]
        self.assertIsNone(detail["drafts"][0]["parent_draft_id"])  # type: ignore[index]
        self.assertEqual(detail["drafts"][0]["id"], detail["drafts"][1]["parent_draft_id"])  # type: ignore[index]

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
        self.assertEqual("openai", job["reviewer_provider"])
        self.assertEqual("fake-content-model", job["reviewer_model"])
        self.assertEqual("completed", job["status"])
        self.assertTrue(all(run["provider"] == "openai" and run["generation_job_id"] == job["id"] for run in generated["runs"]))  # type: ignore[index]
        self.assertEqual("openai", generated["draft"]["provider"])  # type: ignore[index]

    def test_requested_reviewer_route_is_saved_without_replacing_the_writer(self) -> None:
        project_id, asset_id = self.asset()
        status, generated = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/generate",
            {
                "project_id": project_id,
                "provider": "openai",
                "reviewer_provider": "deepseek",
                "reviewer_model": "deepseek-review-v1",
                "target_audience": "US buyers",
                "business_goal": "commercial",
                "sources": [],
            },
        )

        self.assertEqual(201, status)
        job = generated["generation_job"]  # type: ignore[index]
        self.assertEqual("openai", job["provider"])
        self.assertEqual("deepseek", job["reviewer_provider"])
        self.assertEqual("deepseek-review-v1", job["reviewer_model"])
        self.assertEqual("openai", generated["draft"]["provider"])  # type: ignore[index]

    def test_auto_collaboration_keeps_auditable_gpt_writer_and_deepseek_reviewer_route(self) -> None:
        project_id, asset_id = self.asset()
        status, generated = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/generate",
            {
                "project_id": project_id,
                "routing_mode": "auto_collaborate",
                "target_audience": "US buyers",
                "business_goal": "commercial",
                "sources": [],
            },
        )

        self.assertEqual(201, status)
        job = generated["generation_job"]  # type: ignore[index]
        self.assertEqual("auto_collaborate", job["routing_mode"])
        self.assertEqual("openai", job["provider"])
        self.assertEqual("deepseek", job["reviewer_provider"])
        self.assertIn("DeepSeek", job["routing_summary"])
        self.assertIn("ChatGPT", job["routing_summary"])
        self.assertIn("qa", self.generator.stages)
        self.assertEqual("needs_verification", generated["draft"]["qa_status"])  # type: ignore[index]

    def test_generation_uses_only_relevant_project_learning_memory_and_records_the_link(self) -> None:
        project_id, asset_id = self.asset()
        status, memory = self.request(
            "POST",
            "/api/content-learning-memories",
            {
                "project_id": project_id,
                "memory_type": "style",
                "topic": "SEO tools comparison",
                "summary": "Open with a buyer scenario and use a comparison table for SEO tools.",
                "quality_score": 0.9,
                "evidence": {"source": "reviewed competitor structure"},
            },
        )
        self.assertEqual(201, status)

        status, _generated = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/generate",
            {"project_id": project_id, "target_audience": "US buyers", "business_goal": "commercial", "sources": []},
        )

        self.assertEqual(201, status)
        learned = self.generator.stage_inputs["semantic"][0]["learning_memories"]
        self.assertEqual(memory["id"], learned[0]["memory_id"])  # type: ignore[index]
        self.assertNotIn("content", learned[0])
        _, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual(memory["id"], detail["learning_memories"][0]["id"])  # type: ignore[index]
        self.assertEqual("style", detail["learning_memories"][0]["role"])  # type: ignore[index]

    def test_generation_adds_project_gsc_as_private_intent_context(self) -> None:
        project_id, asset_id = self.asset(title_text="Outdoor LED Strip Lights: A Selection Guide")
        connection = sqlite3.connect(self.server.database_path)
        try:
            connection.execute(
                """INSERT INTO project_gsc_query_rows(
                       project_id,property_url,query,page_url,clicks,impressions,ctr,position
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (project_id, "https://example.test", "outdoor led strip lights waterproof", "https://example.test/outdoor-led-strips", 8, 240, 0.033, 9.5),
            )
            connection.commit()
        finally:
            connection.close()

        status, _generated = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/generate",
            {"project_id": project_id, "target_audience": "US buyers", "business_goal": "commercial", "sources": []},
        )

        self.assertEqual(201, status)
        sources = self.generator.stage_inputs["semantic"][0]["sources"]
        gsc_sources = [source for source in sources if source.get("source_type") == "gsc_performance"]
        self.assertEqual(1, len(gsc_sources))
        self.assertEqual("https://example.test/outdoor-led-strips", gsc_sources[0]["url"])
        self.assertIn("Private GSC planning signal", gsc_sources[0]["content"])

    def test_prompt_preview_shows_scoped_source_roles_without_running_a_model(self) -> None:
        project_id, asset_id = self.asset()
        connection = sqlite3.connect(self.server.database_path)
        try:
            connection.execute(
                """INSERT INTO project_gsc_query_rows(
                       project_id,property_url,query,page_url,clicks,impressions,ctr,position
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (project_id, "https://example.test", "seo tools checklist", "https://example.test/seo-tools", 8, 240, 0.033, 9.5),
            )
            connection.commit()
        finally:
            connection.close()

        status, preview = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/prompt-preview",
            {
                "project_id": project_id,
                "target_audience": "US small business owners",
                "business_goal": "commercial research",
                "preview_action": "generate",
                "sources": [{"source_id": "company-catalog", "source_type": "company_knowledge", "title": "Product catalog", "content": "Private source body", "availability": "available"}],
            },
        )

        self.assertEqual(200, status)
        self.assertEqual("people_first_evidence_routed_v20", preview["prompt_version"])  # type: ignore[index]
        self.assertEqual("generate", preview["requested_action"])  # type: ignore[index]
        self.assertIn("evidence-grounded", preview["system_prompt"])  # type: ignore[index]
        self.assertEqual(
            ["industry_rules", "semantic", "title", "outline", "chapter_plan", "section", "assembly", "qa"],
            [stage["stage"] for stage in preview["stages"]],  # type: ignore[index]
        )
        groups = {group["key"]: group for group in preview["source_summary"]}  # type: ignore[index]
        self.assertEqual(1, groups["company_knowledge"]["count"])
        self.assertEqual(1, groups["gsc"]["count"])
        self.assertNotIn("Private source body", str(preview))
        self.assertEqual([], self.generator.stages)

    def test_delete_authority_source_is_committed_and_removed_from_the_project_library(self) -> None:
        project_id, _asset_id = self.asset()
        connection = sqlite3.connect(self.server.database_path)
        try:
            cursor = connection.execute(
                """INSERT INTO authority_source_library(
                       project_id,title,source_type,content,authority_level,tags_json,classification_json
                   ) VALUES(?,?,?,?,?,?,?)""",
                (project_id, "Temporary authority source", "government", "Test-only authority content", "authoritative", "[]", "{}"),
            )
            source_id = cursor.lastrowid
            connection.commit()
        finally:
            connection.close()

        status, deleted = self.request("DELETE", f"/api/authority-sources/{source_id}", {"project_id": project_id})

        self.assertEqual(200, status)
        self.assertEqual(1, deleted["deleted"])  # type: ignore[index]
        status, remaining = self.request("GET", f"/api/authority-sources?project_id={project_id}")
        self.assertEqual(200, status)
        self.assertEqual([], remaining)

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
        self.assertEqual(["industry_rules", "title", "outline", "chapter_plan", "section", "assembly", "content_tags"], self.generator.stages)
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

    def test_quality_review_uses_the_selected_reviewer_route_and_keeps_draft_version(self) -> None:
        project_id, asset_id = self.asset()
        request = {"project_id": project_id, "provider": "openai", "target_audience": "US buyers", "business_goal": "commercial", "sources": []}
        self.assertEqual(201, self.request("POST", f"/api/content-assets/{asset_id}/generate", request)[0])
        self.generator.stages.clear()

        status, reviewed = self.request(
            "POST",
            f"/api/content-assets/{asset_id}/review-quality",
            {**request, "reviewer_provider": "openai", "reviewer_model": "fake-content-model"},
        )

        self.assertEqual(201, status)
        self.assertEqual(["qa"], self.generator.stages)
        self.assertEqual("needs_verification", reviewed["draft"]["qa_status"])  # type: ignore[index]
        self.assertEqual("openai", reviewed["quality_review"]["review"]["reviewer"]["provider"])  # type: ignore[index]
        self.assertEqual("fake-content-model", reviewed["quality_review"]["review"]["reviewer"]["model"])  # type: ignore[index]
        _, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual(1, len(detail["drafts"]))  # type: ignore[index]
        self.assertEqual("needs_verification", detail["current_draft"]["qa_status"])  # type: ignore[index]
        self.assertTrue(any(run["stage"] == "qa" for run in detail["generation_runs"]))  # type: ignore[index]

    def test_targeted_rewrite_creates_a_linked_new_version_and_preserves_the_previous_draft(self) -> None:
        project_id, asset_id = self.asset()
        request = {"project_id": project_id, "provider": "openai", "target_audience": "US buyers", "business_goal": "commercial", "sources": []}
        self.assertEqual(201, self.request("POST", f"/api/content-assets/{asset_id}/generate", request)[0])
        self.assertEqual(201, self.request("POST", f"/api/content-assets/{asset_id}/review-quality", request)[0])
        self.generator.stages.clear()

        status, rewritten = self.request("POST", f"/api/content-assets/{asset_id}/rewrite-targeted", request)

        self.assertEqual(201, status)
        self.assertEqual(["targeted_rewrite"], self.generator.stages)
        self.assertEqual(2, rewritten["draft"]["version"])  # type: ignore[index]
        self.assertIn("Practical verification", rewritten["draft"]["markdown"])  # type: ignore[index]
        _, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual(2, len(detail["drafts"]))  # type: ignore[index]
        self.assertEqual(detail["drafts"][0]["id"], detail["drafts"][1]["parent_draft_id"])  # type: ignore[index]
