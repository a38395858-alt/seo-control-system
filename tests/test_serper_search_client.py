"""Contracts for the configured Serper.dev search integration."""

import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.serper_search_client import SerperSearchClient  # noqa: E402


class _Response:
    def __init__(self, body: dict) -> None:
        self._stream = BytesIO(json.dumps(body).encode())

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self): return self._stream.read()


class SerperSearchClientTests(unittest.TestCase):
    @patch("seo_control.application.serper_search_client.urlopen")
    def test_normalizes_serper_organic_results(self, mock_open) -> None:
        mock_open.return_value = _Response({"organic": [
            {"position": 1, "title": "Official guide", "link": "https://example.gov/guide"},
            {"position": 2, "title": "Duplicate", "link": "https://example.gov/guide#same"},
            {"position": 3, "title": "University research", "link": "https://example.edu/research"},
        ]})
        items = SerperSearchClient("test-key").search(query="IP rating", locale="en-US", max_results=10)
        self.assertEqual(
            [
                {"rank": 1, "title": "Official guide", "url": "https://example.gov/guide", "domain": "example.gov"},
                {"rank": 3, "title": "University research", "url": "https://example.edu/research", "domain": "example.edu"},
            ],
            items,
        )


if __name__ == "__main__":
    unittest.main()
