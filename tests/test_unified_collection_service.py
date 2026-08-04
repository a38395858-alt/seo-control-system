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

from seo_control.application.competitor_intelligence import CollectionService  # noqa: E402
from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.web import create_server  # noqa: E402


class FakeSearchClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, *, query: str, locale: str, max_results: int) -> list[dict[str, object]]:
        self.queries.append(query)
        return [{
            "rank": 1,
            "title": "Editorial guide",
            "url": "https://competitor.test/editorial-guide?utm_source=search",
            "domain": "competitor.test",
        }]


class UnifiedCollectionPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "collection.sqlite3"
        self.connection = initialize_database(self.database_path)
        self.connection.row_factory = __import__("sqlite3").Row
        with self.connection:
            self.project_id = int(self.connection.execute(
                "INSERT INTO projects(name) VALUES('version project')"
            ).lastrowid)
        self.service = CollectionService()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    @staticmethod
    def chunks(content: str) -> list[str]:
        return [content]

    def test_identical_body_reuses_version_and_changed_body_creates_version(self) -> None:
        result = {"url": "https://example.test/guide?utm_source=test", "domain": "example.test", "title": "Guide"}
        first_page = {"domain": "example.test", "title": "Guide", "content": "first article body " * 80}
        changed_page = {"domain": "example.test", "title": "Guide revised", "content": "revised article body " * 80}
        with self.connection:
            first = self.service.persist_content(
                self.connection, project_id=self.project_id, result=result, page=first_page, chunks=self.chunks
            )
            identical = self.service.persist_content(
                self.connection, project_id=self.project_id, result=result, page=first_page, chunks=self.chunks
            )
            changed = self.service.persist_content(
                self.connection, project_id=self.project_id, result=result, page=changed_page, chunks=self.chunks
            )
        self.assertEqual("collected", first.outcome)
        self.assertEqual("unchanged", identical.outcome)
        self.assertEqual(first.version_id, identical.version_id)
        self.assertEqual("collected", changed.outcome)
        self.assertNotEqual(first.version_id, changed.version_id)
        count = self.connection.execute(
            "SELECT COUNT(*) FROM competitor_content_versions WHERE memory_id=?", (first.memory_id,)
        ).fetchone()[0]
        self.assertEqual(2, count)
        current = self.connection.execute(
            "SELECT page_title,content_hash FROM competitor_content_memory WHERE id=?", (first.memory_id,)
        ).fetchone()
        self.assertEqual("Guide revised", current["page_title"])
        self.assertEqual(changed.content_hash, current["content_hash"])

    def test_running_item_is_restored_to_queued_without_replaying_completed_item(self) -> None:
        with self.connection:
            catalog_a = int(self.connection.execute(
                """INSERT INTO competitor_url_catalog(
                       project_id,normalized_url,url,domain,collection_status
                   ) VALUES(?,?,?,?, 'queued')""",
                (self.project_id, "https://example.test/a", "https://example.test/a", "example.test"),
            ).lastrowid)
            catalog_b = int(self.connection.execute(
                """INSERT INTO competitor_url_catalog(
                       project_id,normalized_url,url,domain,collection_status
                   ) VALUES(?,?,?,?, 'collected')""",
                (self.project_id, "https://example.test/b", "https://example.test/b", "example.test"),
            ).lastrowid)
            run_id = int(self.connection.execute(
                """INSERT INTO competitor_catalog_collection_runs(
                       project_id,status,candidate_ids_json,total_count
                   ) VALUES(?,'running','[]',2)""",
                (self.project_id,),
            ).lastrowid)
            self.connection.execute(
                """INSERT INTO collection_run_items(
                       project_id,run_id,catalog_id,normalized_url,source_url,status,attempt_count
                   ) VALUES(?,?,?,?,?,'running',1)""",
                (self.project_id, run_id, catalog_a, "https://example.test/a", "https://example.test/a"),
            )
            self.connection.execute(
                """INSERT INTO collection_run_items(
                       project_id,run_id,catalog_id,normalized_url,source_url,status,attempt_count
                   ) VALUES(?,?,?,?,?,'collected',1)""",
                (self.project_id, run_id, catalog_b, "https://example.test/b", "https://example.test/b"),
            )
            recovered = self.service.recover_interrupted_runs(self.connection)
        self.assertEqual([(self.project_id, run_id)], recovered)
        statuses = [row[0] for row in self.connection.execute(
            "SELECT status FROM collection_run_items WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()]
        self.assertEqual(["queued", "collected"], statuses)
        run = self.connection.execute(
            "SELECT status,recovered_count FROM competitor_catalog_collection_runs WHERE id=?", (run_id,)
        ).fetchone()
        self.assertEqual("queued", run["status"])
        self.assertEqual(1, run["recovered_count"])


class UnifiedCollectionApiIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.search_client = FakeSearchClient()
        self.server = create_server(
            "127.0.0.1", 0,
            database_path=Path(self.temp.name) / "api.sqlite3",
            competitor_search_client=self.search_client,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        request = Request(
            self.base_url + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_collection_plan_and_run_items_are_project_scoped(self) -> None:
        _, first = self.request("POST", "/api/projects", {"name": "first"})
        _, second = self.request("POST", "/api/projects", {"name": "second"})
        first_id, second_id = first["id"], second["id"]  # type: ignore[index]
        status, plan = self.request("POST", "/api/collection-plans", {
            "project_id": first_id, "source_type": "domain", "source_value": "example.test",
        })
        self.assertEqual(200, status)
        self.assertEqual(first_id, plan["project_id"])  # type: ignore[index]
        _, run = self.request("POST", "/api/competitor-url-catalog/collect", {"project_id": first_id})
        run_id = run["id"]  # type: ignore[index]
        status, payload = self.request(
            "GET", f"/api/competitor-url-catalog/collection-runs/{run_id}/items?project_id={second_id}"
        )
        self.assertEqual(400, status)
        self.assertIn("does not exist in this project", payload["error"])  # type: ignore[index]
        _, plans = self.request("GET", f"/api/collection-plans?project_id={second_id}")
        self.assertEqual([], plans)

    def test_domain_and_keyword_plans_discover_into_the_same_deduplicated_catalog(self) -> None:
        _, project = self.request("POST", "/api/projects", {"name": "discovery"})
        project_id = project["id"]  # type: ignore[index]
        _, domain_plan = self.request("POST", "/api/collection-plans", {
            "project_id": project_id, "source_type": "domain", "source_value": "competitor.test",
        })
        status, domain_result = self.request(
            "POST", f"/api/collection-plans/{domain_plan['id']}/discover", {"project_id": project_id}
        )  # type: ignore[index]
        self.assertEqual(200, status)
        self.assertEqual("site:competitor.test", domain_result["query"])  # type: ignore[index]
        _, keyword_plan = self.request("POST", "/api/collection-plans", {
            "project_id": project_id, "source_type": "keyword", "source_value": "outdoor lighting guide",
        })
        status, keyword_result = self.request(
            "POST", f"/api/collection-plans/{keyword_plan['id']}/discover", {"project_id": project_id}
        )  # type: ignore[index]
        self.assertEqual(200, status)
        self.assertEqual(1, keyword_result["queued_count"])  # type: ignore[index]
        _, catalog = self.request("GET", f"/api/competitor-url-catalog?project_id={project_id}")
        self.assertEqual(1, len(catalog))  # type: ignore[arg-type]
        self.assertEqual(2, catalog[0]["discovered_count"])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
