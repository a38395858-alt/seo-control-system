"""Black-box tests for the local keyword-discovery web page."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.web_server import create_server  # noqa: E402


class WebServerTests(unittest.TestCase):
    def test_root_path_serves_the_keyword_discovery_page(self) -> None:
        server = create_server(host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            with urlopen(f"http://{host}:{port}/", timeout=3) as response:
                body = response.read().decode("utf-8")
                self.assertEqual(200, response.status)
                self.assertEqual("text/html", response.headers.get_content_type())
                self.assertEqual("utf-8", response.headers.get_content_charset())
            self.assertIn('id="root"', body)
            self.assertIn('/assets/', body)
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
