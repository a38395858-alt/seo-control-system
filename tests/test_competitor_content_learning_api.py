"""Contracts for the website/project-isolated competitor learning content flow."""

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


class FakeCompetitorClient:
    def __init__(self, count: int = 5) -> None: self.count = count; self.queries: list[str] = []
    def search(self, *, query: str, locale: str, max_results: int) -> list[dict]:
        self.queries.append(query)
        return [{"rank": index, "title": f"Competitor {index}", "url": f"https://competitor-{index}.example/guide", "domain": f"competitor-{index}.example"} for index in range(1, self.count + 1)]
    def extract(self, *, url: str) -> dict[str, str]:
        number = url.split("competitor-")[1].split(".")[0]
        return {"title": f"Competitor guide {number}", "domain": f"competitor-{number}.example", "content": (f"Competitor {number} explains technical selection criteria, maintenance boundaries, parameters and buyer decisions. " * 12)}


class FakeLearningGenerator:
    provider = "openai"; model = "gpt-5.4-fixture"
    def __init__(self) -> None: self.stages: list[str] = []; self.stage_inputs: dict[str, list[dict]] = {}; self.reject_urls: set[str] = set()
    def run_stage(self, *, stage: str, data: dict) -> dict:
        self.stages.append(stage)
        self.stage_inputs.setdefault(stage, []).append(data)
        if stage == "competitor_relevance":
            return {"items": [{"url": page["url"], "decision": "reject" if page["url"] in self.reject_urls else "accept", "reason": "Different search intent." if page["url"] in self.reject_urls else "Same buyer decision.", "learning_focus": ["heading hierarchy"]} for page in data["pages"]]}
        if stage == "competitor_analysis":
            return {"search_intent": "commercial research", "entities": ["IP rating", "maintenance"], "missing_gaps": ["Lifecycle maintenance"], "dynamic_outline": [
                {"heading": "How buyers should compare options", "reader_question": "What matters first?", "purpose": "Set decision criteria", "key_points": ["Compare use cases"], "source_ids": ["competitor-1"], "format": "table"},
                {"heading": "Maintenance and lifecycle planning", "reader_question": "What happens after purchase?", "purpose": "Cover the competitor gap", "key_points": ["Plan maintenance"], "source_ids": ["competitor-2"], "format": "list"},
                {"heading": "Frequently asked questions", "reader_question": "What else do buyers ask?", "purpose": "Answer FAQs", "key_points": ["Answer common questions"], "source_ids": ["competitor-3"], "format": "paragraphs"},
            ], "faq_heading": "Frequently asked questions"}
        if stage == "chapter_plan":
            return {"section_id": data["current_section"]["id"], "writing_goal": "Deepen this buyer decision.", "subtopics": [{"reader_question": data["current_section"].get("reader_question", ""), "points": data["current_section"].get("key_points", []), "source_ids": data["current_section"].get("source_ids", [])}], "must_include": data["current_section"].get("key_points", []), "must_avoid_repeating": ["other chapters"], "format": data["current_section"].get("format", "paragraphs")}
        if stage == "section":
            return {"section_id": data["section"]["id"], "markdown": f"## {data['section']['heading']}\n\nOriginal, source-bounded section guidance. [competitor-1]", "claims_used": [{"claim": "Source-bounded guidance", "source_ids": data["section"].get("source_ids", [])}], "verify": []}
        if stage == "assembly":
            return {"title": data["metadata"]["selected_title"], "meta_description": "Original competitor-informed guide.", "intro_markdown": "Direct answer. [competitor-1]", "conclusion_markdown": "Contact the factory with your requirements. [competitor-10]", "sources_used": ["competitor-1"], "verify": []}
        raise AssertionError(stage)


class PageTwoRecoveryClient(FakeCompetitorClient):
    """Only the first two and an eleventh result are usable content pages."""
    def __init__(self) -> None: super().__init__(count=12)
    def extract(self, *, url: str) -> dict[str, str]:
        number = int(url.split("competitor-")[1].split(".")[0])
        if number not in {1, 2, 11}:
            raise RuntimeError("not an article page")
        return super().extract(url=url)


class CompetitorContentLearningApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.generator = FakeLearningGenerator(); self.client = FakeCompetitorClient()
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temp.name) / "learning.sqlite3", content_generator=self.generator, competitor_content_client=self.client)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start(); self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        request = Request(self.base_url + path, data=None if payload is None else json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=5) as response: return response.status, json.loads(response.read())
        except HTTPError as error: return error.code, json.loads(error.read())

    def asset(self, name: str = "Website A") -> tuple[int, int]:
        _, project = self.request("POST", "/api/projects", {"name": name, "country_code": "US", "language_code": "en"}); project_id = project["id"]  # type: ignore[index]
        self.request("POST", "/api/expanded-keywords", {"project_id": project_id, "seed_keyword": "industrial lights", "country_code": "US", "language_code": "en", "keywords": [{"keyword": "industrial led flood lights", "is_seo_content_fit": True, "same_topic_as_seed": True}]})
        _, keywords = self.request("GET", f"/api/keywords?project_id={project_id}"); keyword_id = keywords[0]["id"]  # type: ignore[index]
        _, title = self.request("POST", "/api/title-candidates", {"project_id": project_id, "keyword_id": keyword_id, "title": "Industrial LED Flood Lights: A B2B Buyer's Guide"})
        self.request("POST", f"/api/title-candidates/{title['id']}/select", {"project_id": project_id})  # type: ignore[index]
        _, content = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title["id"]})  # type: ignore[index]
        return project_id, content["id"]  # type: ignore[index]

    def test_one_click_captures_five_competitors_builds_dynamic_outline_and_saves_memory(self) -> None:
        project_id, asset_id = self.asset()
        status, body = self.request("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "provider": "openai", "competitor_research": True, "target_audience": "US B2B buyers", "business_goal": "lead-generation"})
        self.assertEqual(201, status); self.assertEqual(["Industrial LED Flood Lights: A B2B Buyer's Guide"], self.client.queries)
        self.assertEqual("completed", body["competitor_research"]["status"])  # type: ignore[index]
        self.assertEqual(5, body["competitor_research"]["usable_count"])  # type: ignore[index]
        self.assertEqual(3, len(body["outline"]["sections"]))  # type: ignore[index]
        self.assertEqual("openai", body["generation_job"]["provider"])  # type: ignore[index]
        self.assertIn("competitor_analysis", self.generator.stages)
        self.assertIn("competitor_relevance", self.generator.stages)
        self.assertEqual(3, self.generator.stages.count("chapter_plan"))
        self.assertEqual(3, self.generator.stages.count("section"))
        self.assertNotIn("[competitor-", body["draft"]["markdown"])  # type: ignore[index]
        self.assertEqual(
            [["competitor-1"], ["competitor-2"], ["competitor-3"]],
            [[source["source_id"] for source in item["sources"]] for item in self.generator.stage_inputs["section"]],
        )
        _, memory = self.request("GET", f"/api/content-memory?project_id={project_id}")
        self.assertEqual(5, len(memory))  # type: ignore[arg-type]

    def test_irrelevant_competitor_pages_are_skipped_before_memory_or_analysis(self) -> None:
        self.generator.reject_urls = {"https://competitor-4.example/guide", "https://competitor-5.example/guide"}
        project_id, asset_id = self.asset()
        status, body = self.request("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "provider": "openai", "competitor_research": True})
        self.assertEqual(201, status)
        self.assertEqual(3, body["competitor_research"]["usable_count"])  # type: ignore[index]
        skipped = [item for item in body["competitor_research"]["items"] if item["status"] == "skipped"]  # type: ignore[index]
        self.assertEqual(2, len(skipped))
        self.assertTrue(all(item["memory_id"] is None for item in skipped))
        _, memory = self.request("GET", f"/api/content-memory?project_id={project_id}")
        self.assertEqual(3, len(memory))  # type: ignore[arg-type]

    def test_memory_is_never_visible_to_another_website_project(self) -> None:
        project_a, asset_a = self.asset("A")
        self.assertEqual(201, self.request("POST", f"/api/content-assets/{asset_a}/generate", {"project_id": project_a, "competitor_research": True})[0])
        project_b, _asset_b = self.asset("B")
        _, memory_b = self.request("GET", f"/api/content-memory?project_id={project_b}")
        self.assertEqual([], memory_b)

    def test_insufficient_competitors_stop_before_outline_or_draft(self) -> None:
        self.server.competitor_content_client = FakeCompetitorClient(count=2)
        project_id, asset_id = self.asset()
        status, body = self.request("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "provider": "openai", "competitor_research": True})
        self.assertEqual(422, status); self.assertIn("at least 3", body["error"])  # type: ignore[index]
        _, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertIsNone(detail["current_draft"])  # type: ignore[index]
        self.assertEqual("insufficient", detail["competitor_research"]["status"])  # type: ignore[index]

    def test_page_two_articles_are_considered_before_declaring_sources_insufficient(self) -> None:
        self.server.competitor_content_client = PageTwoRecoveryClient()
        project_id, asset_id = self.asset()
        status, body = self.request("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "provider": "openai", "competitor_research": True})
        self.assertEqual(201, status)
        self.assertEqual(3, body["competitor_research"]["usable_count"])  # type: ignore[index]
        self.assertTrue(any(item["rank"] == 11 and item["status"] == "selected" for item in body["competitor_research"]["items"]))  # type: ignore[index]


if __name__ == "__main__": unittest.main()
