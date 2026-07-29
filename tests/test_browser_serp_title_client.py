"""Red tests for browser-based Google organic-title extraction."""

import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.browser_serp_title_client import BrowserSerpTitleClient  # noqa: E402
from seo_control.application.browser_competitor_content_client import _BingResultParser  # noqa: E402
from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.web import KeywordDiscoveryRequestHandler  # noqa: E402


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

    def test_bing_fallback_parser_reads_only_organic_heading_links(self) -> None:
        parser = _BingResultParser()
        parser.feed('''<ol><li class="b_algo"><a href="https://wrong.example/">site</a><h2><a href="https://example.gov/guide">Official guide</a></h2></li><li class="b_algo"><h2><a href="https://example.edu/research">University research</a></h2></li></ol>''')
        self.assertEqual(
            [
                {"title": "Official guide", "href": "https://example.gov/guide"},
                {"title": "University research", "href": "https://example.edu/research"},
            ],
            parser.items,
        )

    def test_authority_search_rejects_downloads_and_keeps_the_strict_allowlist(self) -> None:
        self.assertTrue(KeywordDiscoveryRequestHandler._authority_download_url("https://example.gov/specification.pdf"))
        self.assertTrue(KeywordDiscoveryRequestHandler._authority_download_url("https://example.edu/data.xlsx?download=1"))
        self.assertFalse(KeywordDiscoveryRequestHandler._authority_download_url("https://example.gov/guidance"))
        self.assertTrue(KeywordDiscoveryRequestHandler._authority_domain_allowed("standards.example.gov"))
        self.assertTrue(KeywordDiscoveryRequestHandler._authority_domain_allowed("en.wikipedia.org"))
        self.assertFalse(KeywordDiscoveryRequestHandler._authority_domain_allowed("example.com"))

    def test_authority_search_uses_the_shared_entity_not_a_list_of_standard_codes(self) -> None:
        terms = KeywordDiscoveryRequestHandler._authority_core_terms(
            "are led strip lights waterproof",
            "IP20, IP54, IP65, IP67, and IP68: What Each Rating Is Good For",
        )
        self.assertEqual("led strip lights waterproof IP Rating", terms)
        self.assertNotIn("IP20", terms)
        self.assertNotIn("IP68", terms)

    def test_authority_search_query_excludes_downloadable_files_before_fetching(self) -> None:
        task = KeywordDiscoveryRequestHandler._authority_google_tasks(
            "## IP20, IP54, IP65, IP67, and IP68: What Each Rating Is Good For",
            "Ignored title",
            "are led strip lights waterproof",
        )[0]
        self.assertIn("-filetype:pdf", task["query"])
        self.assertIn("-filetype:docx", task["query"])
        self.assertIn("-filetype:xlsx", task["query"])
        self.assertIn("led strip lights waterproof IP Rating", task["query"])

    def test_authority_search_skips_document_viewers_even_without_a_pdf_extension(self) -> None:
        self.assertTrue(KeywordDiscoveryRequestHandler._authority_non_article_url("https://records.example.gov/WebLink/DocView.aspx?id=42"))
        self.assertTrue(KeywordDiscoveryRequestHandler._authority_non_article_url("https://example.edu/plugins/generic/pdfJsViewer/pdf.js/web/viewer.html?file=report"))
        self.assertFalse(KeywordDiscoveryRequestHandler._authority_non_article_url("https://example.gov/guidance/ip-ratings"))

    def test_authority_source_relevance_rejects_an_unrelated_university_housing_page(self) -> None:
        accepted, reason = KeywordDiscoveryRequestHandler._authority_source_relevance(
            keyword="are led strip lights waterproof",
            article_title="Are LED Strip Lights Waterproof? IP Ratings Explained",
            section_heading="Water-Resistant vs Waterproof: Why the Difference Matters",
            source_title="Move-In Checklist | University Housing | Illinois",
        )
        self.assertFalse(accepted)
        self.assertIn("only matches 0", reason)

    def test_authority_source_relevance_accepts_a_matching_ip_rating_source(self) -> None:
        accepted, reason = KeywordDiscoveryRequestHandler._authority_source_relevance(
            keyword="are led strip lights waterproof",
            article_title="Are LED Strip Lights Waterproof? IP Ratings Explained",
            section_heading="What an IP Rating Means for LED Strip Lights",
            source_title="IP Ratings for Waterproof LED Strip Lights",
            source_content="An IP rating explains the waterproof protection level for LED strip lights in wet locations.",
        )
        self.assertTrue(accepted)
        self.assertIn("Title and body match", reason)

    def test_authority_audit_persists_an_unusable_url_for_future_exclusion(self) -> None:
        connection = initialize_database(":memory:")
        connection.execute("PRAGMA foreign_keys=OFF")
        KeywordDiscoveryRequestHandler._save_authority_search_audit(
            connection, "run-1", 1, 1,
            [{"section_heading": "Ingress protection", "claim_topic": "IP ratings", "query": "IP rating site:gov", "url": "https://example.gov/guide.pdf", "title": "PDF", "domain": "example.gov", "rank": "1", "status": "skipped", "reason": "download file"}],
        )
        connection.commit()
        self.assertEqual(
            {"https://example.gov/guide.pdf"},
            KeywordDiscoveryRequestHandler._previously_unusable_authority_urls(connection, 1),
        )
        connection.close()
