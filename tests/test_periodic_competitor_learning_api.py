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


class PeriodicCompetitorLearningApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temp.name) / "periodic-learning.sqlite3")
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

    def test_schedule_is_project_scoped_and_returns_empty_cards(self) -> None:
        first, second = self.project("first"), self.project("second")
        status, saved = self.request("POST", f"/api/projects/{first}/competitor-learning", {
            "topics": ["outdoor stair lighting", "LED step light installation"], "interval_days": 14,
            "enabled": False, "provider": "deepseek", "model": "deepseek-chat",
        })
        self.assertEqual(200, status)
        self.assertEqual(["outdoor stair lighting", "LED step light installation"], saved["topics"])  # type: ignore[index]
        status, first_data = self.request("GET", f"/api/projects/{first}/competitor-learning/runs")
        self.assertEqual(200, status)
        self.assertEqual("deepseek", first_data["schedule"]["provider"])  # type: ignore[index]
        self.assertEqual([], first_data["cards"])  # type: ignore[index]
        status, second_data = self.request("GET", f"/api/projects/{second}/competitor-learning/runs")
        self.assertEqual(200, status)
        self.assertEqual([], second_data["schedule"]["topics"])  # type: ignore[index]

    def test_schedule_rejects_invalid_values_and_manual_run_needs_schedule(self) -> None:
        project_id = self.project("first")
        self.assertEqual(400, self.request("POST", f"/api/projects/{project_id}/competitor-learning/run", {})[0])
        self.assertEqual(400, self.request("POST", f"/api/projects/{project_id}/competitor-learning", {"topics": ["a"], "interval_days": 3, "enabled": True})[0])
        self.assertEqual(400, self.request("POST", f"/api/projects/{project_id}/competitor-learning", {"topics": "not a list", "interval_days": 14, "enabled": True})[0])


if __name__ == "__main__":
    unittest.main()
