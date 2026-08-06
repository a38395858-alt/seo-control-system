"""Contracts for Python form-based WordPress draft publishing."""

import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.web import KeywordDiscoveryRequestHandler  # noqa: E402


class WordPressFormPublisherTests(unittest.TestCase):
    def test_windows_encrypted_password_round_trip_does_not_store_plaintext(self) -> None:
        encrypted = KeywordDiscoveryRequestHandler._protect_wordpress_password("correct horse battery staple")
        self.assertTrue(encrypted.startswith("dpapi:"))
        self.assertNotIn("correct horse", encrypted)
        self.assertEqual("correct horse battery staple", KeywordDiscoveryRequestHandler._unprotect_wordpress_password(encrypted))

    def test_extracts_backend_nonce_and_created_post_id(self) -> None:
        page = '<input type="hidden" id="_wpnonce" name="_wpnonce" value="nonce-123"><input name="post_ID" value="42">'
        self.assertEqual("nonce-123", KeywordDiscoveryRequestHandler._wordpress_hidden_value(page, "_wpnonce"))
        self.assertEqual(42, KeywordDiscoveryRequestHandler._wordpress_post_id("https://example.test/wp-admin/post.php?post=42&action=edit", page))

    def test_post_nonce_prefers_the_article_form_over_admin_widgets(self) -> None:
        page = """
            <input name="_wpnonce" value="sidebar-nonce">
            <form id="post"><input name="_wpnonce" value="post-form-nonce"></form>
        """
        self.assertEqual("post-form-nonce", KeywordDiscoveryRequestHandler._wordpress_post_nonce(page))

    def test_markdown_publisher_produces_html_without_rest_specific_data(self) -> None:
        html = KeywordDiscoveryRequestHandler._markdown_to_wordpress_html("# Title\n\n## Section\n\n- Item")
        self.assertIn("<h1>Title</h1>", html)
        self.assertIn("<h2>Section</h2>", html)
        self.assertIn("<ul><li>Item</li></ul>", html)
        self.assertNotIn("wp-json", html)

    def test_markdown_publisher_preserves_tables_emphasis_and_authority_links(self) -> None:
        markdown = """## Compare **LED stair lights**

| Factor | Check |
| --- | --- |
| Weather | [UL guidance](https://www.ul.com/) |

Use **led stair lights outdoor** where suitable.
"""
        html = KeywordDiscoveryRequestHandler._markdown_to_wordpress_html(markdown)
        self.assertIn('<table class="seo-control-table"', html)
        self.assertIn("seo-control-table-wrap", html)
        self.assertIn("overflow-x:auto", html)
        self.assertIn("min-width:640px", html)
        self.assertIn("<th>Factor</th>", html)
        self.assertIn("<td>Weather</td>", html)
        self.assertIn('href="https://www.ul.com/"', html)
        self.assertIn('rel="nofollow noopener noreferrer"', html)
        self.assertIn("<strong>led stair lights outdoor</strong>", html)

    def test_markdown_publisher_keeps_internal_links_followable_and_marks_external_links_nofollow(self) -> None:
        html = KeywordDiscoveryRequestHandler._markdown_to_wordpress_html(
            "[Product page](https://ledsteplight.com/product) and [External reference](https://www.ul.com/guide)",
            internal_site_url="https://ledsteplight.com",
        )
        self.assertIn('href="https://ledsteplight.com/product" target="_blank" rel="noopener noreferrer"', html)
        self.assertIn('href="https://www.ul.com/guide" target="_blank" rel="nofollow noopener noreferrer"', html)

    def test_raw_html_table_is_rebuilt_as_safe_wordpress_table(self) -> None:
        markdown = """<table onclick="steal()"><thead><tr><th scope="col">Item<script>alert(1)</script></th></tr></thead><tbody><tr><th scope="row">Driver</th><td style="color:red" onclick="steal()">Verify model</td></tr></tbody></table>"""
        html = KeywordDiscoveryRequestHandler._markdown_to_wordpress_html(markdown)
        self.assertIn('<figure class="wp-block-table seo-control-table-wrap"', html)
        self.assertIn('<th scope="col">Item</th>', html)
        self.assertIn('<th scope="row">Driver</th>', html)
        self.assertIn("<td>Verify model</td>", html)
        for unsafe in ("onclick", "color:red", "script", "alert(1)"):
            self.assertNotIn(unsafe, html)

    def test_loose_numbered_and_task_lists_render_as_single_semantic_lists(self) -> None:
        markdown = """1. First check

1. Second check

- [ ] Pending inspection
- [x] Documentation complete"""
        html = KeywordDiscoveryRequestHandler._markdown_to_wordpress_html(markdown)
        self.assertEqual(2, html.count("<ol>"))
        self.assertIn("<ol><li>First check</li><li>Second check</li></ol>", html)
        self.assertEqual(2, html.count("<ol>"))
        self.assertIn("<ol><li>Pending inspection</li><li>Documentation complete</li></ol>", html)
        self.assertNotIn('type="checkbox"', html)

    def test_reader_markdown_normalizes_legacy_tables_and_ordered_markers(self) -> None:
        legacy = """<table><tr><th>Factor</th><th>Check</th></tr><tr><td>Rain</td><td>Verify | document</td></tr></table>

1. Inspect

1. Record"""
        markdown = KeywordDiscoveryRequestHandler._sanitize_reader_markdown(legacy)
        self.assertIn("| Factor | Check |", markdown)
        self.assertIn(r"Verify \| document", markdown)
        self.assertNotIn("<table", markdown)
        self.assertIn("1. Inspect", markdown)
        self.assertIn("2. Record", markdown)

    def test_arbitrary_html_remains_escaped_in_wordpress_output(self) -> None:
        html = KeywordDiscoveryRequestHandler._markdown_to_wordpress_html('<div onclick="steal()">Unsafe</div>')
        self.assertIn("&lt;div", html)
        self.assertNotIn("<div", html)

    def test_wordpress_publisher_creates_draft_before_final_publish(self) -> None:
        initial = KeywordDiscoveryRequestHandler._wordpress_post_form(
            "create-nonce", post_id=0, original_status="auto-draft", status="draft",
            title="Article", content="<p>Body</p>", excerpt="Summary",
        )
        final = KeywordDiscoveryRequestHandler._wordpress_post_form(
            "update-nonce", post_id=42, original_status="draft", status="publish",
            title="Article", content="<p>Body with image</p>", excerpt="Summary",
        )
        self.assertEqual("0", initial["post_ID"])
        self.assertEqual("draft", initial["post_status"])
        self.assertEqual("auto-draft", initial["original_post_status"])
        self.assertEqual("42", final["post_ID"])
        self.assertEqual("draft", final["original_post_status"])
        self.assertEqual("publish", final["post_status"])
        self.assertEqual("Publish", final["publish"])

    def test_media_upload_binds_image_to_the_created_post(self) -> None:
        class Response:
            text = '<input name="_wpnonce" value="media-nonce">'

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"data": {"url": "https://example.test/uploads/section.webp"}}

        class Session:
            upload_data: dict[str, str] | None = None

            def get(self, *args: object, **kwargs: object) -> Response:
                return Response()

            def post(self, *args: object, **kwargs: object) -> Response:
                self.upload_data = kwargs["data"]  # type: ignore[assignment]
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "section.webp"
            image.write_bytes(b"test image")
            session = Session()
            url = KeywordDiscoveryRequestHandler._wordpress_upload_media(session, "https://example.test", image, 42)

        self.assertEqual("https://example.test/uploads/section.webp", url)
        self.assertEqual("42", session.upload_data["post_id"] if session.upload_data else None)

    def test_wordpress_error_summary_omits_error_page_css(self) -> None:
        page = """
            <html><head><style>html { background: #f1f1f1; }</style></head>
            <body><p>抱歉，您不能添加附件到此文章。</p></body></html>
        """
        summary = KeywordDiscoveryRequestHandler._wordpress_error_summary(page)
        self.assertEqual("抱歉，您不能添加附件到此文章。", summary)
        self.assertNotIn("background", summary)

    def test_section_images_are_normalized_to_800_by_600_webp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            target = Path(directory) / "section.webp"
            Image.new("RGB", (1200, 800), color=(40, 60, 80)).save(source, format="PNG")
            KeywordDiscoveryRequestHandler._normalize_section_image_to_webp(source, target)
            with Image.open(target) as image:
                self.assertEqual((800, 600), image.size)
                self.assertEqual("WEBP", image.format)

    def test_authority_footer_ignores_unrelated_saved_sources(self) -> None:
        context = {"keyword": "led stair lights outdoor", "article_title": "Outdoor LED stair lighting guide"}
        self.assertTrue(KeywordDiscoveryRequestHandler._authority_reference_is_relevant({"title": "Outdoor LED stair lighting reliability", "section_heading": "Outdoor LED stair lighting reliability"}, **context))
        self.assertFalse(KeywordDiscoveryRequestHandler._authority_reference_is_relevant({"title": "University move-in checklist"}, **context))
        self.assertFalse(KeywordDiscoveryRequestHandler._authority_reference_is_relevant({"title": "Bluetooth sensors with app", "section_heading": "Outdoor LED stair lighting reliability"}, **context))


if __name__ == "__main__":
    unittest.main()
