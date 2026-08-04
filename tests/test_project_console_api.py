"""Regression contracts for project-scoped console data."""

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


class KnowledgeOrganizer:
    provider = "deepseek"
    model = "test-knowledge-organizer"

    def run_stage(self, *, stage: str, data: dict) -> dict:
        if stage != "knowledge_organization":
            raise AssertionError(f"unexpected stage: {stage}")
        return {
            "knowledge_type": data["user_selected_type"],
            "summary": data["content"][:120],
            "tags": ["verified-test-source"],
        }


class ProjectConsoleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server(
            "127.0.0.1",
            0,
            database_path=Path(self.temp.name) / "console.sqlite3",
            content_generator=KnowledgeOrganizer(),
        )
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
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def project(self, name: str) -> int:
        status, body = self.request("POST", "/api/projects", {"name": name, "site_url": f"https://{name}.example", "industry": "lighting", "country_code": "US", "language_code": "en"})
        self.assertEqual(201, status)
        return body["id"]  # type: ignore[index]

    def test_knowledge_documents_are_scoped_to_their_project(self) -> None:
        first, second = self.project("first"), self.project("second")
        status, created = self.request("POST", f"/api/projects/{first}/knowledge", {"title": "Product facts", "content": "IP65 housing for outdoor use.", "knowledge_type": "product"})
        self.assertEqual(201, status)
        self.assertEqual(1, len(self.request("GET", f"/api/projects/{first}/knowledge")[1]))  # type: ignore[arg-type]
        self.assertEqual([], self.request("GET", f"/api/projects/{second}/knowledge")[1])
        self.assertEqual(400, self.request("DELETE", f"/api/projects/{second}/knowledge/{created['id']}", {})[0])  # type: ignore[index]

    def test_project_summary_and_system_tasks_return_real_aggregates(self) -> None:
        project_id = self.project("summary")
        self.request("POST", f"/api/projects/{project_id}/knowledge", {"title": "Company", "content": "Manufacturer profile.", "knowledge_type": "company"})
        status, summaries = self.request("GET", "/api/projects/summary")
        self.assertEqual(200, status)
        summary = next(item for item in summaries if item["id"] == project_id)  # type: ignore[arg-type]
        self.assertEqual(1, summary["knowledge_count"])
        status, tasks = self.request("GET", "/api/system-tasks")
        self.assertEqual(200, status)
        self.assertIsInstance(tasks, list)

    def test_project_can_be_updated_and_deleted_through_repository_backed_api(self) -> None:
        project_id = self.project("editable")
        status, updated = self.request(
            "PUT",
            f"/api/projects/{project_id}",
            {
                "name": "Edited project",
                "site_url": "https://edited.example",
                "industry": "industrial lighting",
                "country_code": "GB",
                "language_code": "en-GB",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("Edited project", updated["name"])  # type: ignore[index]
        self.assertEqual("https://edited.example", updated["site_url"])  # type: ignore[index]
        self.assertEqual("GB", updated["default_country"])  # type: ignore[index]

        status, projects = self.request("GET", "/api/projects")
        self.assertEqual(200, status)
        self.assertEqual("Edited project", next(item for item in projects if item["id"] == project_id)["name"])  # type: ignore[arg-type]

        status, deleted = self.request("DELETE", f"/api/projects/{project_id}")
        self.assertEqual(200, status)
        self.assertEqual({"deleted": project_id}, deleted)
        self.assertFalse(any(item["id"] == project_id for item in self.request("GET", "/api/projects")[1]))  # type: ignore[union-attr]

    def test_project_update_and_delete_reject_missing_project(self) -> None:
        self.assertEqual(400, self.request("PUT", "/api/projects/99999", {"name": "missing"})[0])
        self.assertEqual(400, self.request("DELETE", "/api/projects/99999")[0])


if __name__ == "__main__":
    unittest.main()
