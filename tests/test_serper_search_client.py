"""Contracts for the configured Serper.dev search integration."""

import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError


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

    @patch("seo_control.application.serper_search_client.time.sleep")
    @patch("seo_control.application.serper_search_client.urlopen")
    def test_retries_tls_network_error_with_a_fresh_short_connection(self, mock_open, _mock_sleep) -> None:
        mock_open.side_effect = [
            URLError("TLS closed"),
            _Response({"organic": [{"position": 1, "title": "Recovered", "link": "https://example.com/recovered"}]}),
        ]

        items = SerperSearchClient("test-key").search(query="IP rating", max_results=1)

        self.assertEqual("Recovered", items[0]["title"])
        self.assertEqual(2, mock_open.call_count)
        first_request = mock_open.call_args_list[0].args[0]
        second_request = mock_open.call_args_list[1].args[0]
        self.assertIsNot(first_request, second_request)
        self.assertEqual("close", first_request.get_header("Connection"))
        self.assertEqual("application/json", first_request.get_header("Accept"))


if __name__ == "__main__":
    unittest.main()
