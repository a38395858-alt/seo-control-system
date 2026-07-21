"""Docker-backed contracts for the new B2B Agent platform.

Run explicitly with RUN_PLATFORM_INTEGRATION=1 while Docker Compose is up.
"""

from __future__ import annotations

import json
import os
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


@unittest.skipUnless(os.getenv("RUN_PLATFORM_INTEGRATION") == "1", "requires the local Docker platform")
class PlatformApiContractTests(unittest.TestCase):
    base_url = os.getenv("PLATFORM_API_URL", "http://127.0.0.1:8010")

    def request(self, method: str, path: str, payload: object | None = None) -> tuple[int, object]:
        request = Request(
            self.base_url + path,
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def create_site(self, suffix: str) -> int:
        status, body = self.request(
            "POST",
            "/api/websites",
            {"domain": f"{suffix}.example", "industry": "Industrial LED", "audience": "US procurement", "brand_tone": "technical"},
        )
        self.assertEqual(201, status)
        return body["id"]  # type: ignore[index]

    def delete_site(self, site_id: int) -> None:
        self.assertEqual(200, self.request("DELETE", f"/api/websites/{site_id}")[0])

    def wait_for_task_status(self, task_id: int, expected: str) -> object:
        for _ in range(20):
            status, body = self.request("GET", f"/api/tasks/{task_id}")
            if status == 200 and body["task"]["status"] == expected:  # type: ignore[index]
                return body
            time.sleep(0.1)
        self.fail(f"task {task_id} did not reach {expected}")

    def wait_for_legacy_runs(self) -> list[object]:
        for _ in range(30):
            status, body = self.request("GET", "/api/legacy-imports")
            if status == 200 and len(body) == 2 and all(run["status"] == "completed" for run in body):  # type: ignore[index]
                return body  # type: ignore[return-value]
            time.sleep(0.1)
        self.fail("legacy migration did not complete")

    def test_workspace_sources_title_promises_and_outlines_are_site_isolated(self) -> None:
        marker = uuid4().hex[:10]
        site_a, site_b = self.create_site(f"a-{marker}"), self.create_site(f"b-{marker}")
        try:
            self.assertEqual(200, self.request("PATCH", f"/api/websites/{site_a}", {"product_scope": "IP65 outdoor flood lights", "prohibited_claims": ["guaranteed ranking"]})[0])
            self.assertEqual(201, self.request("POST", f"/api/websites/{site_a}/terms", {"term": "IP65", "kind": "parameter", "definition": "Ingress protection rating", "source_note": "IEC source"})[0])
            self.assertEqual(201, self.request("POST", f"/api/websites/{site_a}/facts", {"title": "Factory capability", "kind": "case", "detail": "OEM/ODM service", "source_note": "approved company profile"})[0])
            self.assertEqual([], self.request("GET", f"/api/websites/{site_b}/terms")[1])
            self.assertEqual([], self.request("GET", f"/api/websites/{site_b}/facts")[1])

            status, task = self.request("POST", "/api/tasks", {"website_id": site_a, "target_keyword": "best industrial led flood lights", "title": "Best Industrial LED Flood Lights: Compare IP Ratings and Pricing"})
            self.assertEqual(202, status)
            task_id = task["id"]  # type: ignore[index]
            status, assessed = self.request("POST", f"/api/tasks/{task_id}/assess-sources")
            self.assertEqual(202, status)
            self.assertIn(assessed["status"], {"queued", "waiting_for_sources"})  # type: ignore[index]
            self.wait_for_task_status(task_id, "waiting_for_sources")

            status, source = self.request("POST", f"/api/tasks/{task_id}/sources", {"source_type": "url", "label": "Official product specification", "url": "https://manufacturer.example/spec", "publisher": "Manufacturer", "availability": "available", "content": "IP65 rating and dimensions are documented."})
            self.assertEqual(201, status)
            self.assertEqual(404, self.request("GET", f"/api/tasks/{task_id}/sources?site_id={site_b}")[0])
            self.assertEqual(404, self.request("GET", f"/api/tasks/{task_id}/outline?site_id={site_b}")[0])
            status, _second = self.request("POST", f"/api/tasks/{task_id}/sources", {"source_type": "url", "label": "Official price list", "url": "https://manufacturer.example/pricing", "publisher": "Manufacturer", "availability": "available", "content": "Current product pricing is documented."})
            self.assertEqual(201, status)
            self.assertEqual(202, self.request("POST", f"/api/tasks/{task_id}/assess-sources")[0])
            self.wait_for_task_status(task_id, "ready_for_outline")

            outline = {"sections": [
                {"heading": "How to compare IP ratings", "reader_question": "Which IP rating fits the site?", "purpose": "Explain protection choices", "key_points": ["Explain IP65 boundary"], "source_item_ids": [source["id"]], "format": "table", "title_promise": "Compare"},
                {"heading": "Compare pricing inputs", "reader_question": "What changes price?", "purpose": "Compare cost factors", "key_points": ["List configuration factors"], "source_item_ids": [source["id"]], "format": "table", "title_promise": "Pricing"},
                {"heading": "Installation fit", "reader_question": "What should buyers check?", "purpose": "Give installation checks", "key_points": ["Check mounting"], "source_item_ids": [source["id"]], "format": "list", "title_promise": ""},
                {"heading": "Maintenance planning", "reader_question": "What needs planning?", "purpose": "Explain maintenance boundaries", "key_points": ["Plan access"], "source_item_ids": [source["id"]], "format": "list", "title_promise": ""},
                {"heading": "Requesting specifications", "reader_question": "What should a buyer request?", "purpose": "Provide a document checklist", "key_points": ["Request datasheets"], "source_item_ids": [source["id"]], "format": "list", "title_promise": ""},
            ]}
            self.assertEqual(200, self.request("PUT", f"/api/tasks/{task_id}/outline", outline)[0])
            self.assertEqual(200, self.request("POST", f"/api/tasks/{task_id}/outline/confirm")[0])

            self.assertEqual(404, self.request("GET", f"/api/tasks/{task_id}?site_id={site_b}")[0])
        finally:
            self.delete_site(site_a)
            self.delete_site(site_b)

    def test_one_workspace_project_cannot_be_shared_or_reassigned_between_sites(self) -> None:
        marker = uuid4().hex[:10]
        site_a, site_b = self.create_site(f"workspace-a-{marker}"), self.create_site(f"workspace-b-{marker}")
        try:
            workspace_project_id = 9_000_000 + int(marker[:4], 16)
            self.assertEqual(
                200,
                self.request("PATCH", f"/api/websites/{site_a}", {"workspace_project_id": workspace_project_id})[0],
            )
            self.assertEqual(
                400,
                self.request("PATCH", f"/api/websites/{site_a}", {"workspace_project_id": workspace_project_id + 1})[0],
            )
            self.assertEqual(
                400,
                self.request("PATCH", f"/api/websites/{site_b}", {"workspace_project_id": workspace_project_id})[0],
            )
        finally:
            self.delete_site(site_a)
            self.delete_site(site_b)

    def test_legacy_import_preview_and_run_are_idempotent(self) -> None:
        status, preview = self.request("POST", "/api/legacy-imports/preview")
        self.assertEqual(200, status)
        self.assertEqual([110, 220], [project["counts"]["keywords"] for project in preview["projects"]])  # type: ignore[index]
        self.assertEqual([16, 24], [project["counts"]["keyword_title_candidates"] for project in preview["projects"]])  # type: ignore[index]
        status, run = self.request("POST", "/api/legacy-imports/run")
        self.assertEqual(202, status)
        self.assertIn(run["status"], {"queued", "completed"})  # type: ignore[index]
        first = self.wait_for_legacy_runs()
        self.assertEqual([110, 220], [item["counts"]["keywords"] for item in first])  # type: ignore[index]
        self.assertEqual(202, self.request("POST", "/api/legacy-imports/run")[0])
        second = self.wait_for_legacy_runs()
        self.assertEqual([110, 220], [item["counts"]["keywords"] for item in second])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
