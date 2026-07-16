"""Contract tests for the AI keyword-review endpoint.

These tests use an injected reviewer so the HTTP API remains deterministic and
does not require an AI credential or network connection.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from seo_control.web import create_server  # noqa: E402


class FakeReviewer:
    """A local reviewer fixture proving the route delegates its work."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def review(
        self, *, seed_keyword: str, keyword: str, language: str
    ) -> dict[str, object]:
        self.calls.append(
            {
                "seed_keyword": seed_keyword,
                "keyword": keyword,
                "language": language,
            }
        )
        return {
            "is_seo_content_fit": True,
            "same_topic_as_seed": True,
            "search_intent": "informational",
            "recommended_action": "create_article",
            "reason": "The keyword is a relevant informational topic.",
            "confidence": 0.93,
        }


class AiKeywordReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.reviewer = FakeReviewer()
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=str(Path(self.tempdir.name) / "seo.db"),
            keyword_reviewer=self.reviewer,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def test_post_reviews_one_keyword_with_injected_reviewer(self) -> None:
        payload = json.dumps(
            {
                "seed_keyword": "seo tools",
                "keyword": "best seo tools for small business",
                "language": "en",
            }
        ).encode("utf-8")
        port = self.server.server_address[1]
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/api/ai-keyword-reviews",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response_body = json.loads(response.read().decode("utf-8"))
        connection.close()

        self.assertEqual(200, response.status)
        review = response_body["review"]
        self.assertEqual(
            {
                "is_seo_content_fit",
                "same_topic_as_seed",
                "search_intent",
                "recommended_action",
                "reason",
                "confidence",
            },
            set(review),
        )
        self.assertTrue(review["is_seo_content_fit"])
        self.assertEqual("informational", review["search_intent"])
        self.assertEqual(
            [
                {
                    "seed_keyword": "seo tools",
                    "keyword": "best seo tools for small business",
                    "language": "en",
                }
            ],
            self.reviewer.calls,
        )


if __name__ == "__main__":
    unittest.main()
