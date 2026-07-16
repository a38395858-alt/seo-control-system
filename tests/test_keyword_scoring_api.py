"""HTTP contract for batch keyword difficulty scoring."""

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


class KeywordScoringApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temporary_directory.name) / "score.sqlite3")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def test_scores_a_keyword_from_ads_and_serp_signals(self) -> None:
        request = Request(
            self.base_url + "/api/keyword-opportunity-scores",
            data=json.dumps(
                {
                    "items": [
                        {
                            "keyword": "seo tools",
                            "monthly_search_volume": 1000,
                            "average_domain_authority": 40,
                            "average_referring_domains": 100,
                            "exact_title_match_rate": 0.3,
                            "authority_site_ratio": 0.2,
                            "intent_competition": 3,
                            "relevance_score": 0.9,
                            "business_value_score": 0.8,
                        }
                    ]
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(200, response.status)
        self.assertEqual(
            [{"keyword": "seo tools", "keyword_difficulty": 43, "difficulty_level": "medium", "opportunity_score": 20}],
            payload["scores"],
        )
