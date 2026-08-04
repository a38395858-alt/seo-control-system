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


class BatchClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def extract(self, *, url: str) -> dict[str, str]:
        self.urls.append(url)
        if "robots" in url:
            raise RuntimeError("Competitor page is blocked by robots.txt.")
        if "fails" in url:
            raise RuntimeError("Competitor HTTP fetch failed: TimeoutError.")
        return {
            "title": "Useful article",
            "domain": "example.test",
            "content": "A usable competitor article about outdoor lighting decisions. " * 20,
        }


class CompetitorCatalogCollectionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = BatchClient()
        self.server = create_server(
            "127.0.0.1", 0,
            database_path=Path(self.temp.name) / "catalog.sqlite3",
            competitor_content_client=self.client,
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
        for _ in range(50):
            _status, runs = self.request("GET", f"/api/competitor-url-catalog/collection-runs?project_id={project_id}")
            run = runs[0]  # type: ignore[index]
            if run["status"] in {"completed", "failed"}:
                return run
            time.sleep(0.03)
        self.fail("bulk collection did not finish")

    def test_collects_every_pending_page_once_and_retries_only_failures(self) -> None:
        _status, project = self.request("POST", "/api/projects", {"name": "catalog project"})
        project_id = project["id"]  # type: ignore[index]
        rows = [
            ("https://example.test/article?rsltid=first", "queued"),
            ("https://example.test/article?utm_source=google", "queued"),
            ("https://robots.example.test/guide", "queued"),
            ("https://fails.example.test/guide", "failed"),
        ]
        connection = initialize_database(self.server.database_path)
        with connection:
            for index, (url, status) in enumerate(rows, 1):
                connection.execute(
                    """INSERT INTO competitor_url_catalog(project_id,normalized_url,url,domain,search_title,collection_status,last_rank)
                       VALUES(?,?,?,?,?,?,?)""",
                    (project_id, f"legacy-{index}", url, "example.test", f"Page {index}", status, index),
                )
        connection.close()

        status, queued = self.request("POST", "/api/competitor-url-catalog/collect", {"project_id": project_id})
        self.assertEqual(202, status)
        self.assertEqual(3, queued["total_count"])  # type: ignore[index]
        finished = self.wait_for_finish(project_id)
        self.assertEqual("completed", finished["status"])
        self.assertEqual(1, finished["collected_count"])
        self.assertEqual(1, finished["robots_blocked_count"])
        self.assertEqual(1, finished["failed_count"])
        self.assertEqual(1, len([url for url in self.client.urls if "article" in url]))

        _status, memory = self.request("GET", f"/api/content-memory?project_id={project_id}")
        self.assertEqual(1, len(memory))  # type: ignore[arg-type]
        _status, catalog = self.request("GET", f"/api/competitor-url-catalog?project_id={project_id}")
        article_rows = [item for item in catalog if "article" in item["url"]]  # type: ignore[union-attr]
        self.assertEqual(["collected", "collected"], [item["collection_status"] for item in article_rows])

        status, retry = self.request("POST", "/api/competitor-url-catalog/collect", {"project_id": project_id})
        self.assertEqual(202, status)
        self.assertEqual(1, retry["total_count"])  # type: ignore[index]
        self.wait_for_finish(project_id)
        self.assertEqual(2, len([url for url in self.client.urls if "fails" in url]))


if __name__ == "__main__":
    unittest.main()
