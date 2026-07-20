"""Contracts for the finished-content library and reversible asset cleanup."""

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

from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.web import create_server  # noqa: E402


class ContentLibraryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "content-library.sqlite3"
        self.server = create_server("127.0.0.1", 0, database_path=self.database_path, ai_settings_path=Path(self.temp.name) / "settings.json")
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

    def asset(self, title: str = "A Practical SEO Tools Guide") -> tuple[int, int, int]:
        _, project = self.request("POST", "/api/projects", {"name": "Content library", "country_code": "US", "language_code": "en"})
        project_id = project["id"]  # type: ignore[index]
        self.request("POST", "/api/expanded-keywords", {"project_id": project_id, "seed_keyword": "seo tools", "country_code": "US", "language_code": "en", "keywords": [{"keyword": title.lower(), "is_seo_content_fit": True, "same_topic_as_seed": True}]})
        _, keywords = self.request("GET", f"/api/keywords?project_id={project_id}")
        _, candidate = self.request("POST", "/api/title-candidates", {"project_id": project_id, "keyword_id": keywords[0]["id"], "title": title})  # type: ignore[index]
        self.request("POST", f"/api/title-candidates/{candidate['id']}/select", {"project_id": project_id})  # type: ignore[index]
        _, asset = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": candidate["id"]})  # type: ignore[index]
        return project_id, candidate["id"], asset["id"]  # type: ignore[index]

    def mark_complete(self, project_id: int, asset_id: int) -> None:
        connection = initialize_database(self.database_path)
        try:
            with connection:
                cursor = connection.execute("INSERT INTO content_drafts(project_id,content_asset_id,version,title,markdown,provider) SELECT project_id,id,1,title_snapshot,'# Finished article','gemini' FROM content_assets WHERE id=?", (asset_id,))
                connection.execute("UPDATE content_assets SET current_draft_id=?,status='in_review' WHERE id=?", (cursor.lastrowid, asset_id))
        finally:
            connection.close()

    def test_content_library_only_lists_assets_with_a_saved_current_draft(self) -> None:
        project_id, _title_id, incomplete_asset_id = self.asset()
        # A second selected title in the same project creates the complete asset.
        _, keywords = self.request("GET", f"/api/keywords?project_id={project_id}")
        _, candidate = self.request("POST", "/api/title-candidates", {"project_id": project_id, "keyword_id": keywords[0]["id"], "title": "A Second SEO Tools Guide"})  # type: ignore[index]
        self.request("POST", f"/api/title-candidates/{candidate['id']}/select", {"project_id": project_id, "confirm_replace": True})  # type: ignore[index]
        _, complete = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": candidate["id"]})  # type: ignore[index]
        self.mark_complete(project_id, complete["id"])  # type: ignore[index]

        status, library = self.request("GET", f"/api/content-library?project_id={project_id}")
        self.assertEqual(200, status)
        self.assertEqual([complete["id"]], [row["id"] for row in library])  # type: ignore[index]
        self.assertNotIn(incomplete_asset_id, [row["id"] for row in library])  # type: ignore[index]
        self.assertEqual("内容完成", library[0]["content_status_label"])  # type: ignore[index]

    def test_title_library_and_content_assets_expose_outline_and_content_status_markers(self) -> None:
        project_id, title_id, asset_id = self.asset()
        self.mark_complete(project_id, asset_id)

        _, titles = self.request("GET", f"/api/title-library?project_id={project_id}")
        title = next(row for row in titles if row["id"] == title_id)  # type: ignore[union-attr]
        self.assertEqual("内容完成", title["content_status_label"])
        _, assets = self.request("GET", f"/api/content-assets?project_id={project_id}")
        self.assertEqual("内容完成", assets[0]["content_status_label"])  # type: ignore[index]
        self.assertEqual("待大纲", assets[0]["outline_status_label"])  # type: ignore[index]

    def test_assets_can_be_soft_deleted_singly_or_in_a_confirmed_batch(self) -> None:
        project_id, _title_id, first_id = self.asset()
        _, keywords = self.request("GET", f"/api/keywords?project_id={project_id}")
        _, candidate = self.request("POST", "/api/title-candidates", {"project_id": project_id, "keyword_id": keywords[0]["id"], "title": "Another Content Asset"})  # type: ignore[index]
        self.request("POST", f"/api/title-candidates/{candidate['id']}/select", {"project_id": project_id, "confirm_replace": True})  # type: ignore[index]
        _, second = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": candidate["id"]})  # type: ignore[index]

        self.assertEqual(200, self.request("DELETE", f"/api/content-assets/{first_id}", {"project_id": project_id})[0])
        _, assets = self.request("GET", f"/api/content-assets?project_id={project_id}")
        self.assertEqual([second["id"]], [item["id"] for item in assets])  # type: ignore[index]
        self.assertEqual(200, self.request("DELETE", "/api/content-assets", {"project_id": project_id, "content_asset_ids": [second["id"]]})[0])  # type: ignore[index]
        _, assets = self.request("GET", f"/api/content-assets?project_id={project_id}")
        self.assertEqual([], assets)

