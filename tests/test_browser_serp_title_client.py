"""Red tests for browser-based Google organic-title extraction."""

import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.browser_serp_title_client import BrowserSerpTitleClient  # noqa: E402


class BrowserSerpTitleClientTests(unittest.TestCase):
    def test_only_visible_google_verification_messages_trigger_a_captcha_state(self) -> None:
        self.assertFalse(BrowserSerpTitleClient.requires_verification("Google search results include a hidden recaptcha script reference."))
        self.assertTrue(BrowserSerpTitleClient.requires_verification("Our systems have detected unusual traffic from your computer network."))

    def test_keeps_organic_result_titles_in_rank_order_and_skips_ads(self) -> None:
        client = BrowserSerpTitleClient()
        results = client.parse_result_items(
            [
                {"title": "Sponsored SEO Service", "href": "https://www.google.com/aclk?x=1", "is_ad": True},
                {"title": "Best AI SEO Tools", "href": "https://example.com/best", "is_ad": False},
                {"title": "Best AI SEO Tools", "href": "https://example.com/duplicate", "is_ad": False},
                {"title": "AI SEO Tools: A Practical Guide", "href": "https://example.org/guide", "is_ad": False},
            ],
            max_count=20,
        )
        self.assertEqual(
            [
                {"rank": 1, "title": "Best AI SEO Tools", "source": "example.com"},
                {"rank": 2, "title": "AI SEO Tools: A Practical Guide", "source": "example.org"},
            ],
            results,
        )
