"""Contract tests for the keyword CSV import HTTP API.

These tests deliberately describe the public API before its handlers exist.  The
service must persist data in the SQLite database passed to ``create_server``;
each test starts a real local HTTP server and talks to it with the standard
library only.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from seo_control.web import create_server


class KeywordImportApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "keyword-import.sqlite3"
        self.server = create_server("127.0.0.1", 0, database_path=self.database_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def request_json(self, method: str, path: str, payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {} if body is None else {"Content-Type": "application/json"}
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def create_project(self) -> int:
        status, payload = self.request_json("POST", "/api/projects", {"name": "Coffee site"})
        self.assertEqual(201, status)
        self.assertIsInstance(payload["id"], int)
        return payload["id"]

    def test_create_project_returns_201_with_persisted_id(self) -> None:
        project_id = self.create_project()

        self.assertGreater(project_id, 0)

    def test_import_csv_and_list_project_keywords(self) -> None:
        project_id = self.create_project()
        csv_text = "".join(
            [
                "keyword,search_volume,competition,cpc\n",
                "coffee grinder,1000,0.80,1.20\n",
                "espresso grinder,500,0.40,0.90\n",
                ",100,0.10,0.20\n",
            ]
        )

        status, result = self.request_json(
            "POST",
            "/api/keyword-imports",
            {
                "project_id": project_id,
                "filename": "google-ads-keywords.csv",
                "csv_text": csv_text,
                "country_code": "US",
                "language_code": "en",
                "metric_date": "2026-07-16",
            },
        )

        self.assertEqual(201, status)
        self.assertEqual(
            {"accepted": 2, "new": 2, "updated": 0, "rejected": 1},
            {name: result[name] for name in ("accepted", "new", "updated", "rejected")},
        )

        status, keywords = self.request_json("GET", f"/api/keywords?project_id={project_id}")

        self.assertEqual(200, status)
        self.assertEqual(2, len(keywords))
        by_keyword = {keyword["keyword"]: keyword for keyword in keywords}
        self.assertEqual({"coffee grinder", "espresso grinder"}, set(by_keyword))
        self.assertEqual("US", by_keyword["coffee grinder"]["country_code"])
        self.assertEqual("en", by_keyword["coffee grinder"]["language_code"])
        self.assertEqual(1000, by_keyword["coffee grinder"]["search_volume"])

    def test_reimport_updates_the_same_keyword_for_its_market(self) -> None:
        project_id = self.create_project()
        import_payload = {
            "project_id": project_id,
            "filename": "keywords.csv",
            "country_code": "US",
            "language_code": "en",
            "metric_date": "2026-07-16",
        }

        first_status, _ = self.request_json(
            "POST",
            "/api/keyword-imports",
            import_payload | {"csv_text": "keyword,search_volume\ncoffee grinder,1000\n"},
        )
        second_status, result = self.request_json(
            "POST",
            "/api/keyword-imports",
            import_payload | {"csv_text": "keyword,search_volume\ncoffee grinder,1200\n"},
        )

        self.assertEqual(201, first_status)
        self.assertEqual(201, second_status)
        self.assertEqual(
            {"accepted": 1, "new": 0, "updated": 1, "rejected": 0},
            {name: result[name] for name in ("accepted", "new", "updated", "rejected")},
        )
        _, keywords = self.request_json("GET", f"/api/keywords?project_id={project_id}")
        self.assertEqual(1, len(keywords))
        self.assertEqual(1200, keywords[0]["search_volume"])


if __name__ == "__main__":
    unittest.main()
