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

from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.web import create_server  # noqa: E402


class LearningGenerator:
    provider = "deepseek"
    model = "learning-fixture"

    def run_stage(self, *, stage: str, data: dict[str, object]) -> dict[str, object]:
        if stage != "competitor_memory_synthesis":
            raise AssertionError(stage)
        sources = data["source_documents"]
        assert isinstance(sources, list)
        return {
            "memory_cards": [{
                "topic": "outdoor stair lighting evaluation",
                "summary": "Start with the reader's installation constraints, then compare power options and finish with a short verification checklist.",
                "source_ids": [source["source_id"] for source in sources],
                "quality_score": 0.81,
            }]
        }


class CollectedCompetitorContentLearningApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server(
            "127.0.0.1", 0,
            database_path=Path(self.temp.name) / "learning.sqlite3",
            content_generator=LearningGenerator(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        request = Request(self.base_url + path, data=None if payload is None else json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def wait_for_finish(self, project_id: int) -> dict:
        for _ in range(70):
            _status, runs = self.request("GET", f"/api/competitor-content-learning/runs?project_id={project_id}")
            run = runs[0]  # type: ignore[index]
            if run["status"] in {"completed", "failed"}:
                return run
            time.sleep(0.03)
        self.fail("collected-content learning did not finish")

    def test_ai_learns_collected_content_once_and_keeps_traceable_sources(self) -> None:
        _status, project = self.request("POST", "/api/projects", {"name": "learning project"})
        project_id = project["id"]  # type: ignore[index]
        connection = initialize_database(self.server.database_path)
        with connection:
            for index in range(2):
                url = f"https://example.test/stair-guide-{index}"
                connection.execute(
                    """INSERT INTO competitor_content_memory(project_id,normalized_url,url,domain,page_title,content,content_hash)
                       VALUES(?,?,?,?,?,?,?)""",
                    (project_id, url, url, "example.test", f"Stair guide {index}", "Useful outdoor stair lighting article. " * 120, f"hash-{index}"),
                )
        connection.close()

        status, queued = self.request("POST", "/api/competitor-content-learning/learn", {"project_id": project_id, "provider": "deepseek"})
        self.assertEqual(202, status)
        self.assertEqual(2, queued["source_count"])  # type: ignore[index]
        finished = self.wait_for_finish(project_id)
        self.assertEqual("completed", finished["status"], finished)
        self.assertEqual(2, finished["processed_count"])
        self.assertEqual(1, finished["memories_created_count"])

        _status, memories = self.request("GET", f"/api/content-learning-memories?project_id={project_id}")
        self.assertEqual(1, len(memories))  # type: ignore[arg-type]
        memory = memories[0]  # type: ignore[index]
        self.assertEqual("style", memory["memory_type"])
        self.assertEqual("collected_competitor_content_library", memory["evidence"]["source"])
        self.assertEqual(2, len(memory["evidence"]["sources"]))

        status, _queued = self.request("POST", "/api/competitor-content-learning/learn", {"project_id": project_id})
        self.assertEqual(202, status)
        repeated = self.wait_for_finish(project_id)
        self.assertEqual(0, repeated["memories_created_count"])


if __name__ == "__main__":
    unittest.main()
