"""Contracts for evidence-gated learning from published-page GSC observations."""

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
from seo_control.web import KeywordDiscoveryRequestHandler, create_server  # noqa: E402


class GscPerformanceLearningApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "gsc-learning.sqlite3"
        self.server = create_server("127.0.0.1", 0, database_path=self.database_path, ai_settings_path=Path(self.temp.name) / "settings.json")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start(); self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        request = Request(self.base_url + path, data=None if payload is None else json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request) as response: return response.status, json.loads(response.read())
        except HTTPError as error: return error.code, json.loads(error.read())

    def published_asset(self) -> tuple[int, int, str]:
        _, project = self.request("POST", "/api/projects", {"name": "GSC learning", "country_code": "US", "language_code": "en"})
        project_id = project["id"]  # type: ignore[index]
        self.request("POST", "/api/expanded-keywords", {"project_id": project_id, "seed_keyword": "led stair lights", "country_code": "US", "language_code": "en", "keywords": [{"keyword": "outdoor led stair lights", "is_seo_content_fit": True, "same_topic_as_seed": True}]})
        _, keywords = self.request("GET", f"/api/keywords?project_id={project_id}")
        _, title = self.request("POST", "/api/title-candidates", {"project_id": project_id, "keyword_id": keywords[0]["id"], "title": "Outdoor LED Stair Lights Guide"})  # type: ignore[index]
        self.request("POST", f"/api/title-candidates/{title['id']}/select", {"project_id": project_id})  # type: ignore[index]
        _, asset = self.request("POST", "/api/content-assets", {"project_id": project_id, "selected_title_candidate_id": title["id"]})  # type: ignore[index]
        asset_id, page_url = asset["id"], "https://example.test/outdoor-led-stair-lights-guide/"  # type: ignore[index]
        connection = initialize_database(self.database_path)
        try:
            with connection:
                draft = connection.execute("INSERT INTO content_drafts(project_id,content_asset_id,version,title,markdown,provider) VALUES(?,?,1,'Outdoor LED Stair Lights Guide','# Guide','openai')", (project_id, asset_id))
                connection.execute("UPDATE content_assets SET current_draft_id=? WHERE id=?", (draft.lastrowid, asset_id))
                connection.execute("INSERT INTO content_wordpress_publications(project_id,content_asset_id,draft_id,wordpress_post_id,wordpress_url,status) VALUES(?,?,?,?,?,'publish')", (project_id, asset_id, draft.lastrowid, 12, page_url))
                for query, clicks, impressions, position in [("outdoor led stair lights", 18, 400, 8.2), ("led stair lighting ideas", 8, 220, 11.0), ("waterproof step lights", 4, 130, 14.5)]:
                    connection.execute("INSERT INTO project_gsc_query_rows(project_id,property_url,query,page_url,clicks,impressions,ctr,position) VALUES(?,?,?,?,?,?,?,?)", (project_id, "https://example.test", query, page_url, clicks, impressions, clicks / impressions, position))
        finally:
            connection.close()
        return project_id, asset_id, page_url

    def test_first_snapshot_observes_and_second_valid_snapshot_creates_performance_memory(self) -> None:
        project_id, asset_id, _url = self.published_asset()
        status, first = self.request("POST", f"/api/projects/{project_id}/gsc/learn-content", {"days": 7})
        self.assertEqual(200, status)
        self.assertEqual("observing", first["snapshots"][0]["learning_status"])  # type: ignore[index]
        self.assertEqual(0, first["memories_created"])  # type: ignore[index]
        status, observing_effectiveness = self.request("GET", f"/api/projects/{project_id}/gsc/content-effectiveness")
        self.assertEqual(200, status)
        observing_article = observing_effectiveness["articles"][0]  # type: ignore[index]
        self.assertEqual(asset_id, observing_article["content_asset_id"])
        self.assertEqual("observing", observing_article["performance_status"])
        self.assertIsNone(observing_article["performance_score"])
        self.assertIsNone(observing_article["combined_score"])
        self.assertIsNone(observing_effectiveness["summary"]["spearman_correlation"])  # type: ignore[index]
        self.assertEqual("insufficient", observing_effectiveness["summary"]["correlation_state"])  # type: ignore[index]

        connection = initialize_database(self.database_path)
        try:
            with connection: connection.execute("UPDATE content_gsc_performance_snapshots SET collected_at=datetime('now','-8 days') WHERE project_id=? AND content_asset_id=?", (project_id, asset_id))
        finally: connection.close()
        status, second = self.request("POST", f"/api/projects/{project_id}/gsc/learn-content", {"days": 7})
        self.assertEqual(200, status)
        self.assertEqual("qualified", second["snapshots"][0]["learning_status"])  # type: ignore[index]
        self.assertEqual(1, second["memories_created"])  # type: ignore[index]
        _, memories = self.request("GET", f"/api/content-learning-memories?project_id={project_id}")
        performance = [memory for memory in memories if memory["memory_type"] == "performance"]  # type: ignore[union-attr]
        self.assertEqual(1, len(performance))
        self.assertEqual("Google Search Console", performance[0]["evidence"]["source"])
        self.assertEqual("descriptive_only", performance[0]["evidence"]["causality"])
        status, qualified_effectiveness = self.request("GET", f"/api/projects/{project_id}/gsc/content-effectiveness")
        self.assertEqual(200, status)
        qualified_article = qualified_effectiveness["articles"][0]  # type: ignore[index]
        self.assertEqual("qualified", qualified_article["performance_status"])
        self.assertIsInstance(qualified_article["performance_score"], int)
        self.assertIsInstance(qualified_article["combined_score"], int)

    def test_only_the_current_project_can_read_its_snapshots(self) -> None:
        project_id, _asset_id, _url = self.published_asset()
        self.request("POST", f"/api/projects/{project_id}/gsc/learn-content", {"days": 7})
        _, other = self.request("POST", "/api/projects", {"name": "Other", "country_code": "US", "language_code": "en"})
        self.assertEqual([], self.request("GET", f"/api/projects/{other['id']}/gsc/content-performance")[1])  # type: ignore[index]
        _, effectiveness = self.request("GET", f"/api/projects/{other['id']}/gsc/content-effectiveness")
        self.assertEqual([], effectiveness["articles"])  # type: ignore[index]
        self.assertEqual(0, effectiveness["summary"]["published_articles"])  # type: ignore[index]

    def test_spearman_correlation_is_only_available_with_five_qualified_articles(self) -> None:
        self.assertIsNone(KeywordDiscoveryRequestHandler._spearman_correlation([(72, 20), (77, 24), (80, 31), (85, 36)]))
        self.assertEqual(1.0, KeywordDiscoveryRequestHandler._spearman_correlation([(60, 10), (70, 20), (80, 30), (90, 40), (100, 45)]))
        self.assertEqual(-1.0, KeywordDiscoveryRequestHandler._spearman_correlation([(60, 45), (70, 40), (80, 30), (90, 20), (100, 10)]))


if __name__ == "__main__":
    unittest.main()
