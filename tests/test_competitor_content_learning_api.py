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
    def __init__(self, count: int = 5) -> None: self.count = count; self.queries: list[str] = []; self.search_limits: list[int] = []
    def search(self, *, query: str, locale: str, max_results: int) -> list[dict]:
        self.queries.append(query); self.search_limits.append(max_results)
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
            source_ids = [page["source_id"] for page in data["competitors_content"]]
            return {"search_intent": "commercial research", "entities": ["IP rating", "maintenance"], "missing_gaps": ["Lifecycle maintenance"], "dynamic_outline": [
                {"heading": "How buyers should compare options", "reader_question": "What matters first?", "purpose": "Set decision criteria", "key_points": ["Compare use cases"], "source_ids": [source_ids[0]], "format": "table"},
                {"heading": "Maintenance and lifecycle planning", "reader_question": "What happens after purchase?", "purpose": "Cover the competitor gap", "key_points": ["Plan maintenance"], "source_ids": [source_ids[min(1, len(source_ids) - 1)]], "format": "list"},
                {"heading": "Frequently asked questions", "reader_question": "What else do buyers ask?", "purpose": "Answer FAQs", "key_points": ["Answer common questions"], "source_ids": [source_ids[min(2, len(source_ids) - 1)]], "format": "paragraphs"},
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


class GoogleThenBingClient(FakeCompetitorClient):
    """Retains a Bing method so Google-only competitor research can prove it was not invoked."""
    def __init__(self) -> None:
        super().__init__(count=2)
        self.bing_queries: list[str] = []; self.bing_limits: list[int] = []

    def search_bing(self, *, query: str, locale: str, max_results: int) -> list[dict]:
        self.bing_queries.append(query); self.bing_limits.append(max_results)
        return [
            {"rank": index, "title": f"Bing competitor {index}", "url": f"https://bing-competitor-{index}.example/article", "domain": f"bing-competitor-{index}.example"}
            for index in range(1, 4)
        ]

    def extract(self, *, url: str) -> dict[str, str]:
        if "bing-competitor" in url:
            number = url.split("bing-competitor-")[1].split(".")[0]
            return {"title": f"Bing editorial guide {number}", "domain": f"bing-competitor-{number}.example", "content": (f"Bing competitor {number} provides a substantive outdoor lighting guide with design choices and installation details. " * 12)}
        return super().extract(url=url)


class AdjacentFixtureComparisonClient(FakeCompetitorClient):
    """One editorial landscape-light guide should remain usable for a stair-light comparison."""
    def __init__(self) -> None: super().__init__(count=4)
    def search(self, *, query: str, locale: str, max_results: int) -> list[dict]:
        items = super().search(query=query, locale=locale, max_results=max_results)
        items[2]["title"] = "Wired vs Solar Landscape Lights: Complete Guide"
        items[3]["title"] = "Shop solar garden lights"
        items[3]["url"] = "https://competitor-4.example/products/solar-light"
        return items
    def extract(self, *, url: str) -> dict[str, str]:
        if "competitor-3" in url:
            return {
                "title": "Wired vs Solar Landscape Lights: Complete Guide",
                "domain": "competitor-3.example",
                "content": ("This outdoor landscape lighting guide compares solar and wired lighting. "
                            "It explains installation, reliability, maintenance, advantages, and selection trade-offs. " * 18),
            }
        if "competitor-4" in url:
            return {"title": "Shop solar garden lights", "domain": "competitor-4.example", "content": "Solar wired lights for sale. " * 80}
        return super().extract(url=url)


class CompetitorContentLearningApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.generator = FakeLearningGenerator(); self.client = FakeCompetitorClient()
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temp.name) / "learning.sqlite3", content_generator=self.generator, competitor_content_client=self.client, competitor_search_client=self.client)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start(); self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        request = Request(self.base_url + path, data=None if payload is None else json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=5) as response: return response.status, json.loads(response.read())
        except HTTPError as error: return error.code, json.loads(error.read())

    def asset(self, name: str = "Website A", title_text: str = "Industrial LED Flood Lights: A B2B Buyer's Guide") -> tuple[int, int]:
        _, project = self.request("POST", "/api/projects", {"name": name, "country_code": "US", "language_code": "en"}); project_id = project["id"]  # type: ignore[index]
        self.request("POST", "/api/expanded-keywords", {"project_id": project_id, "seed_keyword": "industrial lights", "country_code": "US", "language_code": "en", "keywords": [{"keyword": "industrial led flood lights", "is_seo_content_fit": True, "same_topic_as_seed": True}]})
        _, keywords = self.request("GET", f"/api/keywords?project_id={project_id}"); keyword_id = keywords[0]["id"]  # type: ignore[index]
        _, title = self.request("POST", "/api/title-candidates", {"project_id": project_id, "keyword_id": keyword_id, "title": title_text})
        self.request("POST", f"/api/title-candidates/{title['id']}/select", {"project_id": project_id})  # type: ignore[index]
        _, content = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title["id"]})  # type: ignore[index]
        return project_id, content["id"]  # type: ignore[index]

    def test_one_click_captures_five_competitors_builds_dynamic_outline_and_saves_memory(self) -> None:
        project_id, asset_id = self.asset()
        status, body = self.request("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "provider": "openai", "competitor_research": True, "target_audience": "US B2B buyers", "business_goal": "lead-generation"})
        self.assertEqual(201, status); self.assertEqual("Industrial LED Flood Lights: A B2B Buyer's Guide", self.client.queries[0]); self.assertGreaterEqual(len(self.client.queries), 1)
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

    def test_irrelevant_competitor_pages_are_saved_but_skipped_by_the_analysis_pack(self) -> None:
        self.generator.reject_urls = {"https://competitor-4.example/guide", "https://competitor-5.example/guide"}
        project_id, asset_id = self.asset()
        status, body = self.request("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "provider": "openai", "competitor_research": True})
        self.assertEqual(201, status, body)
        self.assertEqual(3, body["competitor_research"]["usable_count"])  # type: ignore[index]
        skipped = [item for item in body["competitor_research"]["items"] if item["status"] == "skipped"]  # type: ignore[index]
        self.assertEqual(2, len(skipped))
        self.assertTrue(all(item["memory_id"] is None for item in skipped))
        _, memory = self.request("GET", f"/api/content-memory?project_id={project_id}")
        self.assertEqual(5, len(memory))  # type: ignore[arg-type]

    def test_memory_is_never_visible_to_another_website_project(self) -> None:
        project_a, asset_a = self.asset("A")
        self.assertEqual(201, self.request("POST", f"/api/content-assets/{asset_a}/generate", {"project_id": project_a, "competitor_research": True})[0])
        project_b, _asset_b = self.asset("B")
        _, memory_b = self.request("GET", f"/api/content-memory?project_id={project_b}")
        self.assertEqual([], memory_b)

    def test_one_competitor_continues_to_outline_and_draft(self) -> None:
        self.server.competitor_content_client = FakeCompetitorClient(count=1); self.server.competitor_search_client = self.server.competitor_content_client
        project_id, asset_id = self.asset()
        status, body = self.request("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "provider": "openai", "competitor_research": True})
        self.assertEqual(201, status, body)
        self.assertEqual(1, body["competitor_research"]["usable_count"])  # type: ignore[index]
        self.assertEqual(1, len(self.generator.stage_inputs["competitor_analysis"][-1]["competitors_content"]))
        _, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertIsNotNone(detail["current_draft"])  # type: ignore[index]
        self.assertEqual("completed", detail["competitor_research"]["status"])  # type: ignore[index]

    def test_zero_competitors_stop_before_outline_or_draft(self) -> None:
        self.server.competitor_content_client = FakeCompetitorClient(count=0); self.server.competitor_search_client = self.server.competitor_content_client
        project_id, asset_id = self.asset()
        status, body = self.request("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "provider": "openai", "competitor_research": True})
        self.assertEqual(422, status); self.assertIn("no readable organic results", body["error"].casefold())  # type: ignore[index]
        _, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertIsNone(detail["current_draft"])  # type: ignore[index]
        self.assertEqual("insufficient", detail["competitor_research"]["status"])  # type: ignore[index]

    def test_all_eligible_result_urls_are_collected_while_analysis_remains_bounded(self) -> None:
        self.server.competitor_content_client = PageTwoRecoveryClient(); self.server.competitor_search_client = self.server.competitor_content_client
        project_id, asset_id = self.asset()
        status, body = self.request("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "provider": "openai", "competitor_research": True})
        self.assertEqual(201, status, body)
        self.assertEqual(3, body["competitor_research"]["usable_count"])  # type: ignore[index]

    def test_google_content_research_uses_top_thirty_without_bing(self) -> None:
        client = GoogleThenBingClient(); client.count = 7; self.server.competitor_content_client = client; self.server.competitor_search_client = client
        project_id, asset_id = self.asset()

        status, body = self.request("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "provider": "openai", "competitor_research": True})

        self.assertEqual(201, status, body)
        self.assertEqual([30], client.search_limits)
        self.assertEqual([], client.bing_queries)
        self.assertEqual(5, body["competitor_research"]["usable_count"])  # type: ignore[index]

    def test_single_title_run_collects_at_most_five_articles_while_cataloging_all_results(self) -> None:
        self.server.competitor_content_client = FakeCompetitorClient(count=7); self.server.competitor_search_client = self.server.competitor_content_client
        project_id, asset_id = self.asset()

        status, body = self.request("POST", f"/api/content-assets/{asset_id}/research-competitors", {"project_id": project_id})

        self.assertEqual(201, status, body)
        self.assertEqual(5, body["usable_count"])  # type: ignore[index]
        _, memory = self.request("GET", f"/api/content-memory?project_id={project_id}")
        self.assertEqual(5, len(memory))  # type: ignore[arg-type]
        _, catalog = self.request("GET", f"/api/competitor-url-catalog?project_id={project_id}")
        self.assertEqual(7, len(catalog))  # type: ignore[arg-type]
        self.assertEqual(5, sum(item["collection_status"] == "collected" for item in catalog))  # type: ignore[index]
        analysis_input = self.generator.stage_inputs["competitor_analysis"][-1]
        self.assertEqual(5, len(analysis_input["competitors_content"]))

    def test_comparison_keeps_substantive_adjacent_fixture_guide_but_rejects_store_page(self) -> None:
        self.server.competitor_content_client = AdjacentFixtureComparisonClient()
        self.server.competitor_search_client = self.server.competitor_content_client
        self.generator.reject_urls = {
            "https://competitor-3.example/guide",
            "https://competitor-4.example/products/solar-light",
        }
        # The adjacent page explains the same solar-versus-wired decision for
        # landscape fixtures, despite the selected fixture being stair lights.
        project_id, asset_id = self.asset(title_text="LED Stair Lights Outdoor Solar vs. Wired: Which Is Right for You?")
        status, body = self.request("POST", f"/api/content-assets/{asset_id}/generate", {"project_id": project_id, "provider": "openai", "competitor_research": True})
        self.assertEqual(201, status, body)
        self.assertEqual("LED Stair Lights Outdoor Solar vs. Wired: Which Is Right for You?", self.server.competitor_search_client.queries[0])
        selected = [item for item in body["competitor_research"]["items"] if item["status"] == "selected"]  # type: ignore[index]
        self.assertTrue(any("competitor-3" in item["url"] for item in selected))
        self.assertTrue(any(item["url"].endswith("/products/solar-light") and item["status"] == "skipped" for item in body["competitor_research"]["items"]))  # type: ignore[index]


if __name__ == "__main__": unittest.main()
