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
if str(SRC_ROOT) not in sys.path: sys.path.insert(0, str(SRC_ROOT))
from seo_control.web import create_server  # noqa: E402


class ContentLearningMemoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.server = create_server("127.0.0.1", 0, database_path=Path(self.temp.name) / "memory.sqlite3")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start(); self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()
    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        request = Request(self.base_url + path, data=None if payload is None else json.dumps(payload).encode(), headers={"Content-Type":"application/json"}, method=method)
        try:
            with urlopen(request) as response: return response.status, json.loads(response.read())
        except HTTPError as error: return error.code, json.loads(error.read())
    def project(self, name: str) -> int:
        status, body = self.request("POST", "/api/projects", {"name": name}); self.assertEqual(201, status); return body["id"]  # type: ignore[index]
    def test_memory_is_scoped_and_can_be_disabled(self) -> None:
        first, second = self.project("first"), self.project("second")
        status, created = self.request("POST", "/api/content-learning-memories", {"project_id": first, "memory_type":"style", "topic":"outdoor stairs", "summary":"Use a scenario-led comparison table.", "quality_score":0.8, "evidence":{"source":"captured article"}})
        self.assertEqual(201, status); self.assertEqual("active", created["status"])  # type: ignore[index]
        self.assertEqual([], self.request("GET", f"/api/content-learning-memories?project_id={second}")[1])
        memory_id = created["id"]  # type: ignore[index]
        self.assertEqual(400, self.request("GET", f"/api/content-learning-memories/{memory_id}?project_id={second}")[0])
        status, disabled = self.request("POST", f"/api/content-learning-memories/{memory_id}/disable", {"project_id": first})
        self.assertEqual(200, status); self.assertEqual("disabled", disabled["status"])  # type: ignore[index]


if __name__ == "__main__": unittest.main()
