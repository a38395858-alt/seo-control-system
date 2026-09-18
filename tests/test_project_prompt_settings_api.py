"""Project-scoped prompt settings API contracts."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.web import create_server  # noqa: E402


class ProjectPromptSettingsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.server = create_server("127.0.0.1", 0, database_path=root / "prompts.sqlite3", ai_settings_path=root / "settings.json")
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
        except Exception as error:
            return error.code, json.loads(error.read())  # type: ignore[attr-defined]

    def project(self, name: str) -> int:
        status, payload = self.request("POST", "/api/projects", {"name": name, "country_code": "US", "language_code": "en"})
        self.assertEqual(201, status)
        return payload["id"]  # type: ignore[index]

    def test_save_read_reset_and_project_isolation(self) -> None:
        first, second = self.project("First site"), self.project("Second site")
        instruction = "Use a calm technical voice for facility managers."

        status, saved = self.request("PUT", f"/api/projects/{first}/prompts/content_generation", {"custom_instruction": instruction})
        self.assertEqual(200, status)
        self.assertTrue(saved["customized"])  # type: ignore[index]

        _, first_payload = self.request("GET", f"/api/projects/{first}/prompts")
        _, second_payload = self.request("GET", f"/api/projects/{second}/prompts")
        first_content = next(item for item in first_payload["prompts"] if item["key"] == "content_generation")  # type: ignore[index]
        second_content = next(item for item in second_payload["prompts"] if item["key"] == "content_generation")  # type: ignore[index]
        self.assertEqual(instruction, first_content["custom_instruction"])
        self.assertEqual("", second_content["custom_instruction"])
        self.assertIn("title", first_content["variables"])

        status, reset = self.request("DELETE", f"/api/projects/{first}/prompts/content_generation")
        self.assertEqual(200, status)
        self.assertFalse(reset["customized"])  # type: ignore[index]

    def test_rejects_unknown_keys_and_oversized_instructions(self) -> None:
        project_id = self.project("Validation site")
        self.assertEqual(404, self.request("PUT", f"/api/projects/{project_id}/prompts/unknown", {"custom_instruction": "x"})[0])
        status, payload = self.request("PUT", f"/api/projects/{project_id}/prompts/keyword_review", {"custom_instruction": "x" * 8001})
        self.assertEqual(400, status)
        self.assertIn("8000", payload["error"])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
