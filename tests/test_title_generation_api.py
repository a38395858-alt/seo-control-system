"""Red contract tests for the title-generation backend.

The title module turns one approved keyword into several traceable candidate
titles.  These tests deliberately use a deterministic local AI fixture: no
real provider, API key, or network request is involved.
"""

from __future__ import annotations

import json
import sqlite3
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

from seo_control.application.browser_serp_title_client import GoogleSerpVerificationRequired  # noqa: E402
from seo_control.web import create_server  # noqa: E402


class FakeTitleGenerator:
    """Returns the JSON contract expected from an OpenAI-compatible adapter."""

    def generate(self, **_request: object) -> str:
        return json.dumps(
            {
                "candidates": [
                    {
                        "title": "How to Choose SEO Tools for a Small Business",
                        "title_type": "tutorial",
                        "primary_keyword_included": True,
                        "search_intent": "informational",
                        "reason": "Matches a US small-business reader looking for a practical guide.",
                    },
                    {
                        "title": "Best SEO Tools for Small Businesses: A 2026 Buyer's Guide",
                        "title_type": "comparison",
                        "primary_keyword_included": True,
                        "search_intent": "commercial",
                        "reason": "Matches comparison intent without promising rankings.",
                    },
                ]
            }
        )

    def research_serp_titles(self, **_request: object) -> str:
        return json.dumps(
            {
                "titles": [
                    {"rank": 1, "title": "Best AI SEO Tools for Websites: 2026 Guide", "source": "Example"},
                    {"rank": 2, "title": "AI SEO Tools for Website Optimization", "source": "Example 2"},
                ]
            }
        )


class FencedSerpTitleGenerator:
    def research_serp_titles(self, **_request: object) -> str:
        return "```json\n{\"titles\":[{\"rank\":1,\"title\":\"AI SEO Tools for Website Optimization\",\"source\":\"Example\"}]}\n```"


class EmptySerpTitleGenerator:
    def research_serp_titles(self, **_request: object) -> str:
        return ""


class FakeBrowserSerpClient:
    def fetch_titles(self, **_request: object) -> list[dict[str, object]]:
        return [
            {"rank": 1, "title": "Best iPhone Screen Call Apps", "source": "example.com"},
            {"rank": 2, "title": "How to Use iPhone Screen Calls", "source": "example.org"},
        ]


class ProviderTitleGenerator:
    def __init__(self, provider: str) -> None:
        self.provider = provider

    def generate(self, **_request: object) -> str:
        return json.dumps({"candidates": [{"title": f"{self.provider} SEO Title {index}", "title_type": "comparison", "search_intent": "commercial", "reason": "Uses the observed Google intent without copying titles."} for index in range(1, 4)]})


class VerificationBrowserSerpClient:
    def fetch_titles(self, **_request: object) -> list[dict[str, object]]:
        raise GoogleSerpVerificationRequired("Google 要求浏览器验证。", "base64-captcha-image")


class TitleGenerationApiTests(unittest.TestCase):
    """Public contracts for generation, selection, replacement, and deletion."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "titles.sqlite3"
        self.server = create_server("127.0.0.1", 0, database_path=self.database_path)
        # The production server will expose this dependency just like
        # ``keyword_reviewer``.  Keeping the fixture on the server avoids a
        # real AI call and proves the returned JSON is persisted.
        self.server.title_generator = FakeTitleGenerator()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

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

    def create_project_and_keyword(self, *, approved: bool = True) -> tuple[int, int]:
        status, project = self.request_json("POST", "/api/projects", {"name": "US SEO site", "country_code": "US", "language_code": "en"})
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
                "keywords": [
                    {
                        "keyword": "seo tools for small business",
                        "is_seo_content_fit": approved,
                        "same_topic_as_seed": True,
                        "search_intent": "informational",
                    }
                ],
            },
        )
        self.assertEqual(201, status)
        status, keywords = self.request_json("GET", f"/api/keywords?project_id={project_id}")
        self.assertEqual(200, status)
        return project_id, keywords[0]["id"]  # type: ignore[index]

    def create_manual_candidate(self, project_id: int, keyword_id: int, title: str) -> int:
        status, candidate = self.request_json(
            "POST",
            "/api/title-candidates",
            {
                "project_id": project_id,
                "keyword_id": keyword_id,
                "title": title,
                "title_type": "tutorial",
                "search_intent": "informational",
            },
        )
        self.assertEqual(201, status)
        return candidate["id"]  # type: ignore[index]

    def test_only_an_approved_keyword_can_start_a_title_generation_job(self) -> None:
        project_id, approved_keyword_id = self.create_project_and_keyword(approved=True)
        status, job = self.request_json(
            "POST",
            "/api/title-generation-jobs",
            {"project_id": project_id, "keyword_id": approved_keyword_id, "locale": "en-US", "count": 2},
        )
        self.assertEqual(201, status)
        self.assertEqual("succeeded", job["status"])  # type: ignore[index]

        pending_project_id, pending_keyword_id = self.create_project_and_keyword(approved=False)
        status, payload = self.request_json(
            "POST",
            "/api/title-generation-jobs",
            {"project_id": pending_project_id, "keyword_id": pending_keyword_id, "locale": "en-US", "count": 2},
        )
        self.assertEqual(400, status)
        self.assertIn("approved", payload["error"].lower())  # type: ignore[index]

    def test_ai_json_candidates_are_saved_under_the_generation_job(self) -> None:
        project_id, keyword_id = self.create_project_and_keyword()
        status, job = self.request_json(
            "POST",
            "/api/title-generation-jobs",
            {"project_id": project_id, "keyword_id": keyword_id, "locale": "en-US", "count": 2},
        )
        self.assertEqual(201, status)
        self.assertEqual(2, job["generated_count"])  # type: ignore[index]

        status, payload = self.request_json("GET", f"/api/keywords/{keyword_id}/title-candidates?project_id={project_id}")
        self.assertEqual(200, status)
        candidates = payload["candidates"]  # type: ignore[index]
        self.assertEqual(2, len(candidates))
        self.assertTrue(all(candidate["source_type"] == "ai" for candidate in candidates))
        self.assertEqual(job["id"], candidates[0]["generation_job_id"])  # type: ignore[index]

    def test_ai_can_extract_up_to_twenty_serp_titles_for_one_approved_keyword(self) -> None:
        project_id, keyword_id = self.create_project_and_keyword()
        status, result = self.request_json(
            "POST",
            "/api/serp-title-research",
            {"project_id": project_id, "keyword_id": keyword_id, "locale": "en-US"},
        )
        self.assertEqual(200, status)
        self.assertEqual("seo tools for small business", result["keyword"])  # type: ignore[index]
        self.assertEqual(2, len(result["titles"]))  # type: ignore[index]
        self.assertEqual(1, result["titles"][0]["rank"])  # type: ignore[index]
        self.assertEqual("Best AI SEO Tools for Websites: 2026 Guide", result["titles"][0]["title"])  # type: ignore[index]

    def test_browser_serp_research_returns_real_browser_extracted_titles(self) -> None:
        project_id, keyword_id = self.create_project_and_keyword()
        self.server.serp_title_client = FakeBrowserSerpClient()
        status, result = self.request_json(
            "POST",
            "/api/browser-serp-title-research",
            {"project_id": project_id, "keyword_id": keyword_id, "locale": "en-US"},
        )
        self.assertEqual(200, status)
        self.assertEqual("browser", result["source_type"])  # type: ignore[index]
        self.assertEqual("Best iPhone Screen Call Apps", result["titles"][0]["title"])  # type: ignore[index]

    def test_browser_serp_research_returns_a_captcha_image_for_user_verification(self) -> None:
        project_id, keyword_id = self.create_project_and_keyword()
        self.server.serp_title_client = VerificationBrowserSerpClient()
        status, result = self.request_json(
            "POST",
            "/api/browser-serp-title-research",
            {"project_id": project_id, "keyword_id": keyword_id, "locale": "en-US"},
        )
        self.assertEqual(200, status)
        self.assertTrue(result["verification_required"])  # type: ignore[index]
        self.assertEqual("base64-captcha-image", result["verification_image"])  # type: ignore[index]

    def test_three_configured_providers_each_generate_three_title_candidates(self) -> None:
        project_id, keyword_id = self.create_project_and_keyword()
        self.server.title_generators = {provider: ProviderTitleGenerator(provider) for provider in ("openai", "gemini", "deepseek")}
        status, job = self.request_json("POST", "/api/multi-provider-title-generation-jobs", {"project_id": project_id, "keyword_id": keyword_id, "locale": "en-US", "competitor_titles": ["Observed Google title"]})
        self.assertEqual(201, status)
        self.assertEqual(9, job["generated_count"])  # type: ignore[index]
        status, payload = self.request_json("GET", f"/api/keywords/{keyword_id}/title-candidates?project_id={project_id}")
        self.assertEqual(200, status)
        reasons = [candidate["reason"] for candidate in payload["candidates"]]  # type: ignore[index]
        self.assertTrue(any(reason.startswith("[ChatGPT]") for reason in reasons))
        self.assertTrue(any(reason.startswith("[Gemini]") for reason in reasons))
        self.assertTrue(any(reason.startswith("[DeepSeek]") for reason in reasons))
        titles = [candidate["title"] for candidate in payload["candidates"]]  # type: ignore[index]
        self.assertEqual(len(titles), len(set(titles)))

    def test_ai_serp_research_accepts_json_wrapped_in_a_markdown_code_block(self) -> None:
        project_id, keyword_id = self.create_project_and_keyword()
        self.server.title_generator = FencedSerpTitleGenerator()
        status, result = self.request_json(
            "POST",
            "/api/serp-title-research",
            {"project_id": project_id, "keyword_id": keyword_id, "locale": "en-US"},
        )
        self.assertEqual(200, status)
        self.assertEqual("AI SEO Tools for Website Optimization", result["titles"][0]["title"])  # type: ignore[index]

    def test_ai_serp_research_returns_a_clear_warning_when_a_provider_returns_empty_content(self) -> None:
        project_id, keyword_id = self.create_project_and_keyword()
        self.server.title_generator = EmptySerpTitleGenerator()
        status, result = self.request_json(
            "POST",
            "/api/serp-title-research",
            {"project_id": project_id, "keyword_id": keyword_id, "locale": "en-US"},
        )
        self.assertEqual(200, status)
        self.assertEqual([], result["titles"])  # type: ignore[index]
        self.assertIn("未返回", result["warning"])  # type: ignore[index]

    def test_generated_title_candidates_are_visible_in_the_project_title_library(self) -> None:
        project_id, keyword_id = self.create_project_and_keyword()
        self.request_json("POST", "/api/title-generation-jobs", {"project_id": project_id, "keyword_id": keyword_id, "locale": "en-US", "count": 2})
        status, titles = self.request_json("GET", f"/api/title-library?project_id={project_id}")
        self.assertEqual(200, status)
        self.assertEqual(2, len(titles))  # type: ignore[arg-type]
        self.assertEqual("seo tools for small business", titles[0]["keyword"])  # type: ignore[index]
        self.assertEqual("candidate", titles[0]["status"])  # type: ignore[index]

    def test_projects_endpoint_recovers_the_latest_project_for_title_history(self) -> None:
        project_id, _keyword_id = self.create_project_and_keyword()
        status, projects = self.request_json("GET", "/api/projects")
        self.assertEqual(200, status)
        self.assertEqual(project_id, projects[0]["id"])  # type: ignore[index]

    def test_database_allows_at_most_one_active_selected_title_per_keyword(self) -> None:
        project_id, keyword_id = self.create_project_and_keyword()
        first_id = self.create_manual_candidate(project_id, keyword_id, "How to Choose SEO Tools for a Small Business")
        second_id = self.create_manual_candidate(project_id, keyword_id, "SEO Tools for Small Businesses: A Practical Guide")
        status, _selected = self.request_json("POST", f"/api/title-candidates/{first_id}/select", {"project_id": project_id})
        self.assertEqual(200, status)
        status, payload = self.request_json("POST", f"/api/title-candidates/{second_id}/select", {"project_id": project_id})
        self.assertEqual(409, status)
        self.assertIn("replace", payload["error"].lower())  # type: ignore[index]

        connection = sqlite3.connect(self.database_path)
        try:
            selected = connection.execute(
                "SELECT id FROM keyword_title_candidates WHERE keyword_id=? AND status='selected' AND deleted_at IS NULL",
                (keyword_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([(first_id,)], selected)

    def test_confirmed_replacement_is_atomic_when_the_audit_event_cannot_be_written(self) -> None:
        project_id, keyword_id = self.create_project_and_keyword()
        first_id = self.create_manual_candidate(project_id, keyword_id, "How to Choose SEO Tools for a Small Business")
        second_id = self.create_manual_candidate(project_id, keyword_id, "SEO Tools for Small Businesses: A Practical Guide")
        status, _selected = self.request_json("POST", f"/api/title-candidates/{first_id}/select", {"project_id": project_id})
        self.assertEqual(200, status)

        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """CREATE TRIGGER reject_title_selection_event
                   BEFORE INSERT ON keyword_title_selection_events
                   BEGIN SELECT RAISE(ABORT, 'forced audit failure'); END"""
            )
            connection.commit()
        finally:
            connection.close()

        status, _payload = self.request_json(
            "POST",
            f"/api/title-candidates/{second_id}/select",
            {"project_id": project_id, "confirm_replace": True},
        )
        self.assertEqual(400, status)
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT id,status FROM keyword_title_candidates WHERE id IN (?,?) ORDER BY id",
                (first_id, second_id),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([(first_id, "selected"), (second_id, "candidate")], rows)

    def test_deleting_the_current_selected_title_is_rejected(self) -> None:
        project_id, keyword_id = self.create_project_and_keyword()
        candidate_id = self.create_manual_candidate(project_id, keyword_id, "How to Choose SEO Tools for a Small Business")
        status, _selected = self.request_json("POST", f"/api/title-candidates/{candidate_id}/select", {"project_id": project_id})
        self.assertEqual(200, status)

        status, payload = self.request_json("DELETE", f"/api/title-candidates/{candidate_id}", {"project_id": project_id})
        self.assertEqual(409, status)
        self.assertIn("selected", payload["error"].lower())  # type: ignore[index]
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute("SELECT status,deleted_at FROM keyword_title_candidates WHERE id=?", (candidate_id,)).fetchone()
        finally:
            connection.close()
        self.assertEqual(("selected", None), row)


if __name__ == "__main__":
    unittest.main()
