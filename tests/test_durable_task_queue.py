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
from seo_control.infrastructure.task_queue import DurableTaskQueue  # noqa: E402
from seo_control.web import create_server  # noqa: E402


class DurableTaskQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "queue.sqlite3"
        connection = initialize_database(self.database_path)
        with connection:
            self.project_id = int(connection.execute("INSERT INTO projects(name) VALUES('Queue project')").lastrowid)
        connection.close()
        self.queue = DurableTaskQueue(
            self.database_path,
            callback_url="http://127.0.0.1:9",
            worker_token="test-worker-token",
            backend="local",
            concurrency=1,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_active_dedup_retry_and_terminal_failure(self) -> None:
        first = self.queue.enqueue(
            project_id=self.project_id,
            task_type="agent_job",
            resource_id=41,
            max_attempts=2,
        )
        repeated = self.queue.enqueue(
            project_id=self.project_id,
            task_type="agent_job",
            resource_id=41,
            max_attempts=2,
        )
        self.assertEqual(first["id"], repeated["id"])
        claimed = self.queue.claim(int(first["id"]))
        self.assertEqual(1, claimed["attempt_count"])  # type: ignore[index]
        waiting = self.queue.fail(int(first["id"]), RuntimeError("temporary upstream failure"))
        self.assertEqual("retry_wait", waiting["status"])
        connection = initialize_database(self.database_path)
        with connection:
            connection.execute("UPDATE durable_task_queue SET available_at=CURRENT_TIMESTAMP WHERE id=?", (first["id"],))
        connection.close()
        self.assertIsNotNone(self.queue.claim(int(first["id"])))
        failed = self.queue.fail(int(first["id"]), RuntimeError("second failure"))
        self.assertEqual("failed", failed["status"])
        self.assertEqual(2, failed["attempt_count"])

    def test_recovery_and_project_boundary(self) -> None:
        queued = self.queue.enqueue(
            project_id=self.project_id,
            task_type="competitor_learning",
            resource_id=7,
        )
        self.assertIsNotNone(self.queue.claim(int(queued["id"])))
        self.assertEqual(1, self.queue.recover_interrupted())
        recovered = self.queue.get(int(queued["id"]))
        self.assertEqual("queued", recovered["status"])
        self.assertIn("Worker stopped", recovered["last_error"])
        with self.assertRaisesRegex(ValueError, "project does not exist"):
            self.queue.enqueue(project_id=self.project_id + 999, task_type="agent_job", resource_id=1)


class DurableTaskQueueInternalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temp.name) / "api.sqlite3")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_internal_execution_rejects_missing_worker_token(self) -> None:
        request = Request(
            self.base_url + "/api/internal/task-queue/999/execute",
            data=json.dumps({}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(403, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
