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


class LearningMemoryFeedbackApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temp.name) / "feedback.sqlite3")
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
        status, body = self.request("POST", "/api/projects", {"name": name})
        self.assertEqual(201, status)
        return body["id"]  # type: ignore[index]

    def memory(self, project_id: int, *, memory_type: str = "style") -> dict:
        status, body = self.request("POST", "/api/content-learning-memories", {
            "project_id": project_id, "memory_type": memory_type, "topic": "outdoor stair lighting",
            "summary": "Start with safety and compare installation choices in a compact table.",
            "quality_score": 0.72, "evidence": {"source": "reviewed article", "metric": 3},
        })
        self.assertEqual(201, status)
        return body  # type: ignore[return-value]

    def test_edit_pin_and_feedback_preserve_evidence(self) -> None:
        project_id = self.project("first")
        memory = self.memory(project_id, memory_type="performance")
        memory_id = memory["id"]
        status, changed = self.request("PUT", f"/api/content-learning-memories/{memory_id}", {
            "project_id": project_id, "topic": "stair-lighting performance", "summary": "Use the successful query pattern as a hypothesis.",
            "manual_priority": 6, "evidence": {"source": "tampered"},
        })
        self.assertEqual(200, status)
        self.assertEqual(6, changed["manual_priority"])  # type: ignore[index]
        self.assertEqual({"source": "reviewed article", "metric": 3}, changed["evidence"])  # type: ignore[index]
        self.assertEqual(200, self.request("POST", f"/api/content-learning-memories/{memory_id}/pin", {"project_id": project_id})[0])
        self.assertEqual(200, self.request("POST", f"/api/content-learning-memories/{memory_id}/feedback", {"project_id": project_id, "decision": "useful", "note": "Matched the article intent."})[0])
        self.assertEqual(200, self.request("POST", f"/api/content-learning-memories/{memory_id}/feedback", {"project_id": project_id, "decision": "not_useful"})[0])
        status, detail = self.request("GET", f"/api/content-learning-memories/{memory_id}?project_id={project_id}")
        self.assertEqual(200, status)
        self.assertEqual(1, detail["pinned"])  # type: ignore[index]
        self.assertEqual(1, detail["positive_feedback_count"])  # type: ignore[index]
        self.assertEqual(1, detail["negative_feedback_count"])  # type: ignore[index]
        self.assertEqual(2, len(detail["feedback"]))  # type: ignore[arg-type]

    def test_actions_and_feedback_are_project_scoped(self) -> None:
        first, second = self.project("first"), self.project("second")
        memory = self.memory(first)
        memory_id = memory["id"]
        self.assertEqual(400, self.request("PUT", f"/api/content-learning-memories/{memory_id}", {"project_id": second, "summary": "cross project edit"})[0])
        self.assertEqual(400, self.request("POST", f"/api/content-learning-memories/{memory_id}/feedback", {"project_id": second, "decision": "useful"})[0])
        self.assertEqual(400, self.request("POST", f"/api/content-learning-memories/{memory_id}/unpin", {"project_id": second})[0])
        self.assertEqual([], self.request("GET", f"/api/content-learning-memories?project_id={second}")[1])

    def test_invalid_priority_and_feedback_are_rejected(self) -> None:
        project_id = self.project("first")
        memory = self.memory(project_id)
        memory_id = memory["id"]
        self.assertEqual(400, self.request("PUT", f"/api/content-learning-memories/{memory_id}", {"project_id": project_id, "manual_priority": 11})[0])
        self.assertEqual(400, self.request("POST", f"/api/content-learning-memories/{memory_id}/feedback", {"project_id": project_id, "decision": "delete"})[0])


if __name__ == "__main__":
    unittest.main()
