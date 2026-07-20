"""Contracts for safe multi-select soft deletion in the title library."""

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


class TitleCandidateBatchDeleteApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temp.name) / "titles.sqlite3", ai_settings_path=Path(self.temp.name) / "settings.json")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        request = Request(self.base_url + path, data=None if payload is None else json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request) as response: return response.status, json.loads(response.read())
        except Exception as error: return error.code, json.loads(error.read())  # type: ignore[attr-defined]

    def candidates(self) -> tuple[int, int, int]:
        _, project = self.request("POST", "/api/projects", {"name": "Delete titles", "country_code": "US", "language_code": "en"})
        project_id = project["id"]  # type: ignore[index]
        self.request("POST", "/api/expanded-keywords", {"project_id": project_id, "seed_keyword": "seo", "country_code": "US", "language_code": "en", "keywords": [{"keyword": "seo tools", "is_seo_content_fit": True, "same_topic_as_seed": True}]})
        _, keywords = self.request("GET", f"/api/keywords?project_id={project_id}")
        first_status, first = self.request("POST", "/api/title-candidates", {"project_id": project_id, "keyword_id": keywords[0]["id"], "title": "First SEO Title"})  # type: ignore[index]
        second_status, second = self.request("POST", "/api/title-candidates", {"project_id": project_id, "keyword_id": keywords[0]["id"], "title": "Second SEO Title"})  # type: ignore[index]
        self.assertEqual((201, 201), (first_status, second_status))
        return project_id, first["id"], second["id"]  # type: ignore[index]

    def test_batch_delete_soft_deletes_nonselected_titles(self) -> None:
        project_id, first, second = self.candidates()
        status, deleted = self.request("DELETE", "/api/title-candidates", {"project_id": project_id, "candidate_ids": [first, second]})
        self.assertEqual(200, status)
        self.assertEqual(2, deleted["deleted"])  # type: ignore[index]
        _, library = self.request("GET", f"/api/title-library?project_id={project_id}")
        self.assertEqual([], library)

    def test_batch_delete_rejects_a_selected_title_without_deleting_other_candidates(self) -> None:
        project_id, selected, other = self.candidates()
        self.request("POST", f"/api/title-candidates/{selected}/select", {"project_id": project_id})
        status, _payload = self.request("DELETE", "/api/title-candidates", {"project_id": project_id, "candidate_ids": [selected, other]})
        self.assertEqual(409, status)
        _, library = self.request("GET", f"/api/title-library?project_id={project_id}")
        self.assertEqual({selected, other}, {row["id"] for row in library})  # type: ignore[arg-type]

