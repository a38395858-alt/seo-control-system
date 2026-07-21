"""Red contract tests for the content-system MVP."""

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


class ContentSystemApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temp.name) / "content.sqlite3")
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

    def selected_title(self) -> tuple[int, int]:
        _, project = self.request("POST", "/api/projects", {"name": "Content MVP", "country_code": "US", "language_code": "en"})
        project_id = project["id"]  # type: ignore[index]
        self.request("POST", "/api/expanded-keywords", {"project_id": project_id, "seed_keyword": "seo tools", "country_code": "US", "language_code": "en", "keywords": [{"keyword": "seo tools for small business", "is_seo_content_fit": True, "same_topic_as_seed": True}]})
        _, keywords = self.request("GET", f"/api/keywords?project_id={project_id}")
        keyword_id = keywords[0]["id"]  # type: ignore[index]
        _, candidate = self.request("POST", "/api/title-candidates", {"project_id": project_id, "keyword_id": keyword_id, "title": "SEO Tools for Small Businesses: A Practical Guide"})
        self.request("POST", f"/api/title-candidates/{candidate['id']}/select", {"project_id": project_id})  # type: ignore[index]
        return project_id, candidate["id"]  # type: ignore[index]

    def test_selected_title_creates_content_asset_brief_and_outline(self) -> None:
        project_id, title_id = self.selected_title()
        status, asset = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title_id, "content_type": "guide"})
        self.assertEqual(201, status)
        self.assertEqual("planned", asset["status"])  # type: ignore[index]
        asset_id = asset["id"]  # type: ignore[index]
        status, brief = self.request("POST", f"/api/content-assets/{asset_id}/briefs", {"project_id": project_id, "target_audience": "US small business owners", "business_goal": "commercial", "target_length": 1400, "sources": []})
        self.assertEqual(201, status)
        self.assertEqual("US small business owners", brief["target_audience"])  # type: ignore[index]
        status, outline = self.request("POST", f"/api/content-assets/{asset_id}/outlines", {"project_id": project_id, "sections": [{"heading": "How to compare SEO tools", "purpose": "Give buyers practical criteria", "word_budget": 500}]})
        self.assertEqual(201, status)
        self.assertEqual(1, len(outline["sections"]))  # type: ignore[index]
        status, detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")
        self.assertEqual(200, status)
        self.assertEqual("SEO Tools for Small Businesses: A Practical Guide", detail["title_snapshot"])  # type: ignore[index]
        self.assertEqual("outlining", detail["status"])  # type: ignore[index]

    def test_unselected_title_cannot_create_content_asset(self) -> None:
        project_id, title_id = self.selected_title()
        self.request("POST", f"/api/title-candidates/{title_id}/select", {"project_id": project_id, "confirm_replace": True})
        status, _body = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title_id + 999})
        self.assertEqual(400, status)

    def test_recreating_content_for_the_same_selected_title_returns_the_existing_asset(self) -> None:
        project_id, title_id = self.selected_title()
        status, first = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title_id})
        self.assertEqual(201, status)
        status, second = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title_id})
        self.assertEqual(200, status)
        self.assertEqual(first["id"], second["id"])  # type: ignore[index]

    def test_manual_brief_is_unbounded_when_the_ui_omits_target_length(self) -> None:
        """字数不是用户配置项；旧的手工保存入口也必须能正常保存。"""
        project_id, title_id = self.selected_title()
        status, asset = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title_id})
        self.assertEqual(201, status)
        status, brief = self.request(
            "POST",
            f"/api/content-assets/{asset['id']}/briefs",  # type: ignore[index]
            {"project_id": project_id, "target_audience": "US buyers", "business_goal": "commercial", "sources": []},
        )
        self.assertEqual(201, status)
        self.assertEqual(0, brief["target_length"])  # type: ignore[index]

    def test_unfinished_assets_are_excluded_from_content_library_but_the_reader_route_loads(self) -> None:
        project_id, title_id = self.selected_title()
        status, asset = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title_id})
        self.assertEqual(201, status)

        status, library = self.request("GET", f"/api/content-library?project_id={project_id}")
        self.assertEqual(200, status)
        self.assertEqual([], library)

        with urlopen(Request(f"{self.base_url}/content-library/{asset['id']}")) as response:  # type: ignore[index]
            self.assertEqual(200, response.status)
            self.assertIn("text/html", response.headers["Content-Type"])
