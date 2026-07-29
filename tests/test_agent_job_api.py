"""Contracts for durable, project-scoped agent task APIs."""

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


class AgentJobApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temp.name) / "agent-jobs.sqlite3")
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
        status, body = self.request("POST", "/api/projects", {"name": name, "country_code": "US", "language_code": "en"})
        self.assertEqual(201, status)
        return body["id"]  # type: ignore[index]

    def test_job_is_project_scoped_and_keeps_langgraph_bootstrap_steps(self) -> None:
        first, second = self.project("first"), self.project("second")
        status, created = self.request("POST", "/api/agent-jobs", {"project_id": first, "requested_action": "generate_content"})
        self.assertEqual(201, status)
        self.assertEqual("queued", created["status"])  # type: ignore[index]

        status, jobs = self.request("GET", f"/api/agent-jobs?project_id={first}")
        self.assertEqual(200, status)
        self.assertEqual(1, len(jobs))  # type: ignore[arg-type]
        self.assertEqual([], self.request("GET", f"/api/agent-jobs?project_id={second}")[1])

        job_id = created["id"]  # type: ignore[index]
        status, detail = self.request("GET", f"/api/agent-jobs/{job_id}?project_id={first}")
        self.assertEqual(200, status)
        self.assertEqual(["validate_project_context", "prepare_workflow"], [step["node_name"] for step in detail["steps"]])  # type: ignore[index]
        self.assertEqual(400, self.request("GET", f"/api/agent-jobs/{job_id}?project_id={second}")[0])

    def test_job_keeps_explicit_writer_and_reviewer_routes_for_later_nodes(self) -> None:
        project_id = self.project("model route")
        status, job = self.request("POST", "/api/agent-jobs", {
            "project_id": project_id,
            "requested_action": "generate_content",
            "writer_provider": "deepseek",
            "writer_model": "deepseek-v4-pro",
            "reviewer_provider": "openai",
            "reviewer_model": "gpt-5.6",
        })
        self.assertEqual(201, status)
        route = job["input"]  # type: ignore[index]
        self.assertEqual("deepseek", route["writer_provider"])
        self.assertEqual("deepseek-v4-pro", route["writer_model"])
        self.assertEqual("openai", route["reviewer_provider"])
        self.assertEqual("gpt-5.6", route["reviewer_model"])

    def test_approval_transitions_job_without_exposing_any_publish_action(self) -> None:
        project_id = self.project("approval")
        status, created = self.request("POST", "/api/agent-jobs", {
            "project_id": project_id,
            "requested_action": "prepare_publish",
            "approval_type": "publish",
            "approval_payload": {"requested_status": "publish"},
        })
        self.assertEqual(201, status)
        self.assertEqual("waiting_approval", created["status"])  # type: ignore[index]

        status, decided = self.request("POST", f"/api/agent-approvals/{created['approval_id']}", {"project_id": project_id, "decision": "approved"})  # type: ignore[index]
        self.assertEqual(200, status)
        self.assertEqual("queued", decided["status"])  # type: ignore[index]
        self.assertNotIn("wordpress_password", json.dumps(decided))

    def test_user_can_cancel_then_retry_a_job(self) -> None:
        project_id = self.project("retry")
        _status, created = self.request("POST", "/api/agent-jobs", {"project_id": project_id, "requested_action": "content_blueprint"})
        job_id = created["id"]  # type: ignore[index]
        status, cancelled = self.request("POST", f"/api/agent-jobs/{job_id}/cancel", {"project_id": project_id})
        self.assertEqual(200, status)
        self.assertEqual("cancelled", cancelled["status"])  # type: ignore[index]
        status, retried = self.request("POST", f"/api/agent-jobs/{job_id}/retry", {"project_id": project_id})
        self.assertEqual(200, status)
        self.assertEqual("queued", retried["status"])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
