"""Contracts for the approval-bound WordPress publication gate."""

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

from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.web import create_server  # noqa: E402


class PublishGateApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "publish-gate.sqlite3"
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
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def asset(self, name: str = "Publish gate") -> tuple[int, int]:
        _, project = self.request("POST", "/api/projects", {"name": name, "country_code": "US", "language_code": "en"})
        project_id = project["id"]  # type: ignore[index]
        self.request("POST", "/api/expanded-keywords", {"project_id": project_id, "seed_keyword": "led stair lights", "country_code": "US", "language_code": "en", "keywords": [{"keyword": "led stair lights", "is_seo_content_fit": True, "same_topic_as_seed": True}]})
        _, keywords = self.request("GET", f"/api/keywords?project_id={project_id}")
        _, title = self.request("POST", "/api/title-candidates", {"project_id": project_id, "keyword_id": keywords[0]["id"], "title": "LED Stair Lights Guide"})  # type: ignore[index]
        self.request("POST", f"/api/title-candidates/{title['id']}/select", {"project_id": project_id})  # type: ignore[index]
        _, asset = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title["id"]})  # type: ignore[index]
        return project_id, asset["id"]  # type: ignore[index]

    def make_publishable(
        self,
        project_id: int,
        asset_id: int,
        *,
        qa_status: str = "approved",
        unresolved: list[str] | None = None,
        markdown: str = "## Selection table\n\n| Factor | Check |\n| --- | --- |\n| IP rating | Verify |",
        meta_description: str = "A practical LED stair lighting guide covering selection, placement, safety checks, and installation planning.",
    ) -> int:
        connection = initialize_database(self.database_path)
        try:
            with connection:
                cursor = connection.execute(
                    """INSERT INTO content_drafts(project_id,content_asset_id,version,title,meta_description,markdown,qa_status,unresolved_verify_json,provider)
                       VALUES(?,?,1,'LED Stair Lights Guide',?,?,?,?, 'openai')""",
                    (project_id, asset_id, meta_description, markdown, qa_status, json.dumps(unresolved or [])),
                )
                draft_id = int(cursor.lastrowid)
                connection.execute("UPDATE content_assets SET current_draft_id=?,tags_json=? WHERE id=?", (draft_id, json.dumps(["LED Lighting", "Buying Guide"]), asset_id))
                connection.execute("INSERT INTO project_wordpress_configs(project_id,site_url,username,application_password,last_tested_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)", (project_id, "https://example.test", "editor", "not-used-by-gate"))
                return draft_id
        finally:
            connection.close()

    def test_gate_does_not_evaluate_qa_or_unresolved_verification(self) -> None:
        project_id, asset_id = self.asset()
        self.make_publishable(project_id, asset_id, qa_status="needs_revision", unresolved=["check an IP claim"])
        status, result = self.request("POST", f"/api/content-assets/{asset_id}/prepare-publish", {"project_id": project_id, "status": "draft", "allow_without_images": True})
        self.assertEqual(200, status)
        self.assertEqual("ready", result["status"])  # type: ignore[index]
        issue_codes = {item["code"] for item in result["report"]["issues"]}  # type: ignore[index]
        check_codes = {item["code"] for item in result["report"]["checks"]}  # type: ignore[index]
        self.assertNotIn("verification", issue_codes | check_codes)
        self.assertNotIn("qa", issue_codes | check_codes)
        self.assertIsInstance(result.get("approval_id"), int)  # type: ignore[union-attr]

    def test_ready_gate_creates_fixed_approval_and_old_draft_cannot_publish(self) -> None:
        project_id, asset_id = self.asset()
        first_draft = self.make_publishable(project_id, asset_id)
        status, prepared = self.request("POST", f"/api/content-assets/{asset_id}/prepare-publish", {"project_id": project_id, "status": "draft", "allow_without_images": True})
        self.assertEqual(200, status)
        self.assertEqual("ready", prepared["status"])  # type: ignore[index]
        self.assertIsInstance(prepared.get("approval_id"), int)  # type: ignore[union-attr]
        approval_id = prepared["approval_id"]  # type: ignore[index]
        self.assertEqual(200, self.request("POST", f"/api/agent-approvals/{approval_id}", {"project_id": project_id, "decision": "approved"})[0])

        connection = initialize_database(self.database_path)
        try:
            with connection:
                cursor = connection.execute("INSERT INTO content_drafts(project_id,content_asset_id,version,title,meta_description,markdown,qa_status,unresolved_verify_json,provider) VALUES(?,?,2,'Revised LED Stair Lights Guide','A revised description that remains long enough for the publication metadata gate.','# Revised','approved','[]','openai')", (project_id, asset_id))
                connection.execute("UPDATE content_assets SET current_draft_id=? WHERE id=?", (cursor.lastrowid, asset_id))
        finally:
            connection.close()
        status, error = self.request("POST", f"/api/content-assets/{asset_id}/publish-wordpress", {"project_id": project_id, "status": "draft", "approval_id": approval_id})
        self.assertEqual(400, status)
        self.assertIn("different content version", error["error"])  # type: ignore[index]

        connection = initialize_database(self.database_path)
        try:
            row = connection.execute("SELECT payload_json,consumed_at FROM agent_approval_requests WHERE id=?", (approval_id,)).fetchone()
            self.assertIsNotNone(row)
            payload = json.loads(row[0])
            self.assertEqual(first_draft, payload["draft_id"])
            self.assertEqual(asset_id, payload["content_asset_id"])
            self.assertEqual("draft", payload["requested_status"])
            self.assertIsNone(row[1])
        finally:
            connection.close()

    def test_publish_endpoint_never_accepts_an_unapproved_or_cross_project_approval(self) -> None:
        first_project, asset_id = self.asset("first")
        self.make_publishable(first_project, asset_id)
        _, prepared = self.request("POST", f"/api/content-assets/{asset_id}/prepare-publish", {"project_id": first_project, "status": "publish", "allow_without_images": True})
        approval_id = prepared["approval_id"]  # type: ignore[index]
        second_project, _second_asset = self.asset("second")
        self.assertEqual(400, self.request("POST", f"/api/agent-approvals/{approval_id}", {"project_id": second_project, "decision": "approved"})[0])
        status, error = self.request("POST", f"/api/content-assets/{asset_id}/publish-wordpress", {"project_id": first_project, "status": "publish", "approval_id": approval_id})
        self.assertEqual(400, status)
        self.assertIn("approved WordPress publish request", error["error"])  # type: ignore[index]

    def test_prepare_is_idempotent_and_exposes_the_gate_snapshot_without_secrets(self) -> None:
        project_id, asset_id = self.asset("idempotent")
        self.make_publishable(project_id, asset_id)
        _, first = self.request("POST", f"/api/content-assets/{asset_id}/prepare-publish", {"project_id": project_id, "status": "draft", "allow_without_images": True})
        _, second = self.request("POST", f"/api/content-assets/{asset_id}/prepare-publish", {"project_id": project_id, "status": "draft", "allow_without_images": True})
        self.assertEqual(first["approval_id"], second["approval_id"])  # type: ignore[index]
        self.assertEqual(first["job_id"], second["job_id"])  # type: ignore[index]
        self.assertTrue(second["reused"])  # type: ignore[index]
        status, detail = self.request("GET", f"/api/agent-jobs/{first['job_id']}?project_id={project_id}")  # type: ignore[index]
        self.assertEqual(200, status)
        approval = detail["approvals"][0]  # type: ignore[index]
        self.assertEqual("publish", approval["approval_type"])
        self.assertEqual("ready", approval["payload"]["report"]["status"])
        serialized = json.dumps(detail).lower()
        self.assertNotIn("application_password", serialized)
        self.assertNotIn("not-used-by-gate", serialized)

    def test_gate_keeps_missing_metadata_as_a_warning_but_blocks_unsafe_markdown_links(self) -> None:
        project_id, asset_id = self.asset("unsafe")
        self.make_publishable(
            project_id,
            asset_id,
            meta_description="",
            markdown="## Unsafe link\n\n[Open this](javascript:alert(1))",
        )
        status, result = self.request("POST", f"/api/content-assets/{asset_id}/prepare-publish", {"project_id": project_id, "status": "publish", "allow_without_images": True})
        self.assertEqual(200, status)
        self.assertEqual("blocked", result["status"])  # type: ignore[index]
        issue_codes = {item["code"] for item in result["report"]["issues"]}  # type: ignore[index]
        self.assertEqual({"links"}, issue_codes)
        metadata_check = next(item for item in result["report"]["checks"] if item["code"] == "meta_description")  # type: ignore[index]
        self.assertFalse(metadata_check["passed"])
        self.assertNotIn("approval_id", result)  # type: ignore[operator]


if __name__ == "__main__":
    unittest.main()
