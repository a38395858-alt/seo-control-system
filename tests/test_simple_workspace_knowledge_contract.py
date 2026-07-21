"""Red contracts for the simplified site-first workspace console."""

from __future__ import annotations

import json
import os
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


@unittest.skipUnless(os.getenv("RUN_PLATFORM_INTEGRATION") == "1", "requires the local Docker platform")
class SimpleWorkspaceKnowledgeContracts(unittest.TestCase):
    base_url = os.getenv("PLATFORM_API_URL", "http://127.0.0.1:8010")

    def request(self, method: str, path: str, payload: object | None = None) -> tuple[int, object]:
        request = Request(self.base_url + path, data=None if payload is None else json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_site_owns_a_workbench_project_and_knowledge_library(self) -> None:
        marker = uuid4().hex[:10]
        status, site = self.request("POST", "/api/websites", {"domain": f"knowledge-{marker}.example", "industry": "B2B controls", "workspace_project_id": 9876})
        self.assertEqual(201, status)
        site_id = site["id"]  # type: ignore[index]
        try:
            self.assertEqual(9876, site["workspace_project_id"])  # type: ignore[index]
            status, document = self.request("POST", f"/api/websites/{site_id}/knowledge", {"title": "Approved factory profile", "source_type": "upload", "content": "We provide OEM controls. Verify certifications with the current datasheet."})
            self.assertEqual(201, status)
            self.assertEqual("ready", document["status"])  # type: ignore[index]
            status, context = self.request("GET", f"/api/websites/{site_id}/knowledge/context")
            self.assertEqual(200, status)
            self.assertIn("Approved factory profile", context["content"])  # type: ignore[index]
        finally:
            self.request("DELETE", f"/api/websites/{site_id}")

