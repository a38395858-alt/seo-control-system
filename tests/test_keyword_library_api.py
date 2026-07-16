"""Contract tests for saving, categorizing and safely removing expanded terms."""

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


class KeywordLibraryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temporary_directory.name) / "library.sqlite3")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def request_json(self, method: str, path: str, payload: dict | None = None):
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

    def test_saves_reviewed_expanded_keywords_categories_and_soft_deletes_them(self) -> None:
        _, project = self.request_json("POST", "/api/projects", {"name": "SEO site", "country_code": "US", "language_code": "en"})
        project_id = project["id"]
        status, saved = self.request_json(
            "POST",
            "/api/expanded-keywords",
            {
                "project_id": project_id,
                "country_code": "US",
                "language_code": "en",
                "seed_keyword": "seo tools",
                "keywords": [
                    {
                        "keyword": "best seo tools",
                        "category": "工具评测",
                        "search_intent": "commercial",
                        "is_seo_content_fit": True,
                        "same_topic_as_seed": True,
                        "review_reason": "Relevant comparison topic.",
                        "review_confidence": 0.91,
                        "demand_estimate": 52,
                    }
                ],
            },
        )
        self.assertEqual(201, status)
        self.assertEqual({"inserted": 1, "existing": 0}, {key: saved[key] for key in ("inserted", "existing")})

        _, keywords = self.request_json("GET", f"/api/keywords?project_id={project_id}")
        self.assertEqual(1, len(keywords))
        self.assertEqual("工具评测", keywords[0]["category"])
        self.assertEqual("commercial", keywords[0]["search_intent"])
        self.assertEqual(52, keywords[0]["demand_estimate"])

        status, deleted = self.request_json("DELETE", "/api/keywords", {"project_id": project_id, "keyword_ids": [keywords[0]["id"]]})
        self.assertEqual(200, status)
        self.assertEqual(1, deleted["deleted"])
        _, after_delete = self.request_json("GET", f"/api/keywords?project_id={project_id}")
        self.assertEqual([], after_delete)

    def test_clearing_a_project_requires_an_explicit_project_confirmation(self) -> None:
        _, project = self.request_json("POST", "/api/projects", {"name": "SEO site"})
        project_id = project["id"]
        status, payload = self.request_json("DELETE", "/api/keywords", {"project_id": project_id, "clear_all": True})
        self.assertEqual(400, status)
        self.assertIn("confirm_project_id", payload["error"])
