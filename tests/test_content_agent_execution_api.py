"""Executable P3 content Agent contracts: approval, recovery, QA and evidence."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.web import create_server  # noqa: E402
from seo_control.infrastructure.database import initialize_database  # noqa: E402
from tests.test_content_generation_workflow_api import FakeContentGenerator  # noqa: E402


class RewriteUntilHumanGenerator(FakeContentGenerator):
    """Always asks for a focused rewrite so the two-attempt gate is exercised."""

    def generate(self, **request: object) -> str:
        stage = request.get("stage")
        if stage == "qa":
            return json.dumps({
                "status": "needs_revision",
                "checks": [{"name": "specificity", "status": "revise", "note": "Add one clearer trade-off."}],
                "targeted_rewrite": [{"target": "How to compare SEO tools", "instruction": "Add one concrete trade-off."}],
                "unresolved_verify": [],
            })
        if stage == "targeted_rewrite":
            return json.dumps({
                "markdown": "# SEO Tools for Small Businesses: A Practical Guide\n\n## How to compare SEO tools\n\nCompare workflow fit and implementation trade-offs.",
                "meta_description": "A practical framework for comparing SEO tools.",
                "applied_targets": ["How to compare SEO tools"],
                "verify": [],
            })
        return super().generate(**request)


class ContentAgentExecutionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database_path = root / "content-agent.sqlite3"
        self.server = create_server(
            "127.0.0.1", 0, database_path=self.database_path,
            ai_settings_path=root / "ai-settings.json",
        )
        self.server.content_generator = RewriteUntilHumanGenerator()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        request = Request(
            self.base_url + path,
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers={} if payload is None else {"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def create_asset(self, project_name: str = "Agent project") -> tuple[int, int]:
        status, project = self.request("POST", "/api/projects", {"name": project_name, "country_code": "US", "language_code": "en"})
        self.assertEqual(201, status)
        project_id = project["id"]  # type: ignore[index]
        self.assertEqual(201, self.request("POST", "/api/expanded-keywords", {
            "project_id": project_id, "country_code": "US", "language_code": "en", "seed_keyword": "seo tools",
            "keywords": [{"keyword": "seo tools for small business", "is_seo_content_fit": True, "same_topic_as_seed": True}],
        })[0])
        keywords = self.request("GET", f"/api/keywords?project_id={project_id}")[1]
        keyword_id = keywords[0]["id"]  # type: ignore[index]
        status, title = self.request("POST", "/api/title-candidates", {
            "project_id": project_id, "keyword_id": keyword_id,
            "title": "SEO Tools for Small Businesses: A Practical Guide",
        })
        self.assertEqual(201, status)
        title_id = title["id"]  # type: ignore[index]
        self.assertEqual(200, self.request("POST", f"/api/title-candidates/{title_id}/select", {"project_id": project_id})[0])
        status, asset = self.request("POST", "/api/content-assets", {
            "project_id": project_id, "selected_title_candidate_id": title_id, "content_type": "guide",
        })
        self.assertEqual(201, status)
        return project_id, asset["id"]  # type: ignore[index]

    def wait_for_job(self, project_id: int, job_id: int, statuses: set[str], timeout: float = 12) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            status, value = self.request("GET", f"/api/agent-jobs/{job_id}?project_id={project_id}")
            self.assertEqual(200, status)
            last = value  # type: ignore[assignment]
            if last.get("status") in statuses:
                return last
            time.sleep(0.05)
        self.fail(f"agent job did not reach {statuses}; last state: {last}")

    def start_agent(self, project_id: int, asset_id: int) -> dict:
        status, job = self.request("POST", "/api/agent-jobs", {
            "project_id": project_id, "content_asset_id": asset_id,
            "requested_action": "full_content_agent", "writer_provider": "gemini",
            "target_audience": "US small business owners", "business_goal": "commercial",
        })
        self.assertEqual(201, status)
        return job  # type: ignore[return-value]

    def test_blueprint_must_be_approved_and_rejection_restores_asset(self) -> None:
        project_id, asset_id = self.create_asset()
        other_project_id, _ = self.create_asset("Other project")
        job = self.start_agent(project_id, asset_id)
        waiting = self.wait_for_job(project_id, job["id"], {"waiting_approval", "failed"})
        self.assertEqual("waiting_approval", waiting["status"], waiting.get("error_summary"))
        self.assertEqual("blueprint_approval", waiting["current_node"])
        approval = waiting["approvals"][0]

        detail = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")[1]
        self.assertIsNone(detail["current_draft"])  # type: ignore[index]
        self.assertIsNone(detail["brief"])  # type: ignore[index]
        self.assertEqual(400, self.request("POST", f"/api/agent-approvals/{approval['id']}", {
            "project_id": other_project_id, "decision": "approved",
        })[0])
        status, rejected = self.request("POST", f"/api/agent-approvals/{approval['id']}", {
            "project_id": project_id, "decision": "rejected", "decided_by": "test",
        })
        self.assertEqual(200, status)
        self.assertEqual("cancelled", rejected["status"])  # type: ignore[index]
        restored = self.request("GET", f"/api/content-assets/{asset_id}?project_id={project_id}")[1]
        self.assertIsNone(restored["current_draft"])  # type: ignore[index]
        self.assertIsNone(restored["brief"])  # type: ignore[index]

    def test_approved_agent_stops_after_two_rewrites_and_writes_basis_report(self) -> None:
        project_id, asset_id = self.create_asset()
        self.assertEqual(201, self.request("POST", "/api/content-learning-memories", {
            "project_id": project_id, "memory_type": "style", "topic": "SEO tools small business",
            "summary": "Use practical comparisons and explicit trade-offs.", "quality_score": 0.9,
            "source_url": "https://example.com/competitor-guide",
        })[0])
        job = self.start_agent(project_id, asset_id)
        waiting = self.wait_for_job(project_id, job["id"], {"waiting_approval", "failed"})
        self.assertEqual("waiting_approval", waiting["status"], waiting.get("error_summary"))
        approval_id = waiting["approvals"][0]["id"]
        self.assertEqual(200, self.request("POST", f"/api/agent-approvals/{approval_id}", {
            "project_id": project_id, "decision": "approved", "decided_by": "test",
        })[0])
        finished = self.wait_for_job(project_id, job["id"], {"waiting_input", "failed", "completed"}, timeout=20)
        self.assertEqual("waiting_input", finished["status"], finished.get("error_summary"))
        self.assertEqual("human_review_required", finished["current_node"])
        self.assertEqual(2, finished["checkpoint"]["rewrite_count"])
        self.assertEqual(3, len(finished["checkpoint"]["qa_history"]))
        self.assertIsNotNone(finished["basis_report"])
        self.assertEqual(2, finished["basis_report"]["qa"]["rewrite_count"])
        self.assertTrue(finished["basis_report"]["memories"])
        self.assertIn("Confidence", finished["basis_report"]["memories"][0]["selection_reason"])
        self.assertNotIn("api_key", json.dumps(finished["basis_report"]).lower())

    def test_restart_closes_interrupted_manual_generation_and_agent_steps(self) -> None:
        project_id, asset_id = self.create_asset()
        connection = initialize_database(self.database_path)
        with connection:
            manual_job_id = connection.execute(
                """INSERT INTO content_generation_jobs(project_id,content_asset_id,requested_action,provider,status)
                   VALUES(?,?, 'generate', 'openai', 'running')""",
                (project_id, asset_id),
            ).lastrowid
            durable_job_id = connection.execute(
                """INSERT INTO content_generation_jobs(project_id,content_asset_id,requested_action,provider,status)
                   VALUES(?,?, 'full_content_agent', 'openai', 'running')""",
                (project_id, asset_id),
            ).lastrowid
            run_id = connection.execute(
                """INSERT INTO content_generation_runs(project_id,content_asset_id,generation_job_id,stage,provider,status)
                   VALUES(?,?,?, 'semantic', 'openai', 'running')""",
                (project_id, asset_id, durable_job_id),
            ).lastrowid
            agent_job_id = connection.execute(
                """INSERT INTO agent_jobs(project_id,content_asset_id,requested_action,status,current_node,workflow_version)
                   VALUES(?,?, 'full_content_agent', 'waiting_approval', 'blueprint_approval', 'content-agent-v2')""",
                (project_id, asset_id),
            ).lastrowid
            step_id = connection.execute(
                "INSERT INTO agent_steps(job_id,node_name,status,started_at) VALUES(?, 'generate_article', 'running', CURRENT_TIMESTAMP)",
                (agent_job_id,),
            ).lastrowid
        connection.close()

        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.server = create_server(
            "127.0.0.1", 0, database_path=self.database_path,
            ai_settings_path=Path(self.temp.name) / "ai-settings.json",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        connection = initialize_database(self.database_path)
        manual = connection.execute("SELECT status,failed_stage,error_summary FROM content_generation_jobs WHERE id=?", (manual_job_id,)).fetchone()
        durable = connection.execute("SELECT status FROM content_generation_jobs WHERE id=?", (durable_job_id,)).fetchone()
        run = connection.execute("SELECT status,error_summary,completed_at FROM content_generation_runs WHERE id=?", (run_id,)).fetchone()
        step = connection.execute("SELECT status,error_summary,completed_at FROM agent_steps WHERE id=?", (step_id,)).fetchone()
        connection.close()
        self.assertEqual(("failed", "interrupted"), (manual[0], manual[1]))
        self.assertIn("service restart", manual[2])
        self.assertEqual("running", durable[0])
        self.assertEqual("failed", run[0])
        self.assertIn("service restart", run[1])
        self.assertIsNotNone(run[2])
        self.assertEqual("failed", step[0])
        self.assertIn("service restart", step[1])
        self.assertIsNotNone(step[2])


if __name__ == "__main__":
    unittest.main()
