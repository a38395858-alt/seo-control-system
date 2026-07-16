"""Red tests for persisting a reviewed keyword-import preview.

The preview deliberately uses plain dictionaries: parsing and previewing happen
upstream, and this service is responsible only for the transactional write.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.keyword_import_service import KeywordImportService


class KeywordImportServiceTests(unittest.TestCase):
    """Contract tests for the keyword CSV/XLSX import persistence boundary."""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self._create_schema()
        self.service = KeywordImportService(self.connection)
        self.project_id = self.service.create_project("VPN China", "CN", "zh-CN")

    def tearDown(self) -> None:
        self.connection.close()

    def test_valid_rows_create_keyword_batch_source_and_metric_snapshot(self) -> None:
        result = self.service.import_preview(
            self.project_id,
            self._preview(
                self._valid_row(keyword="  VPN 推荐  ", monthly_searches=1200),
            ),
            original_filename="vpn-keywords.csv",
            metric_date="2026-07-16",
        )

        self.assertEqual(result.accepted_rows, 1)
        self.assertEqual(result.rejected_rows, 0)

        keyword = self.connection.execute(
            """
            SELECT keyword, normalized_keyword, country_code, language_code
            FROM keywords
            WHERE project_id = ?
            """,
            (self.project_id,),
        ).fetchone()
        self.assertEqual(dict(keyword), {
            "keyword": "VPN 推荐",
            "normalized_keyword": "vpn 推荐",
            "country_code": "CN",
            "language_code": "zh-CN",
        })

        batch = self.connection.execute(
            """
            SELECT original_filename, metric_date, status, total_rows,
                   accepted_rows, rejected_rows
            FROM import_batches
            """
        ).fetchone()
        self.assertEqual(dict(batch), {
            "original_filename": "vpn-keywords.csv",
            "metric_date": "2026-07-16",
            "status": "completed",
            "total_rows": 1,
            "accepted_rows": 1,
            "rejected_rows": 0,
        })

        source = self.connection.execute(
            "SELECT source_type, import_batch_id FROM keyword_sources"
        ).fetchone()
        self.assertEqual(source["source_type"], "file_import")
        self.assertIsNotNone(source["import_batch_id"])

        snapshot = self.connection.execute(
            """
            SELECT source_type, metric_date, country_code, language_code,
                   average_monthly_searches, competition_level,
                   competition_index, low_top_of_page_bid_micros,
                   high_top_of_page_bid_micros
            FROM keyword_metric_snapshots
            """
        ).fetchone()
        self.assertEqual(dict(snapshot), {
            "source_type": "file_import",
            "metric_date": "2026-07-16",
            "country_code": "CN",
            "language_code": "zh-CN",
            "average_monthly_searches": 1200,
            "competition_level": "MEDIUM",
            "competition_index": 45,
            "low_top_of_page_bid_micros": 1000000,
            "high_top_of_page_bid_micros": 2500000,
        })

    def test_reimport_of_same_normalized_keyword_and_market_reuses_keyword(self) -> None:
        first = self.service.import_preview(
            self.project_id,
            self._preview(self._valid_row(keyword="VPN 推荐", monthly_searches=1200)),
            original_filename="july.csv",
            metric_date="2026-07-16",
        )
        second = self.service.import_preview(
            self.project_id,
            self._preview(self._valid_row(keyword=" vpn   推荐 ", monthly_searches=1500)),
            original_filename="august.csv",
            metric_date="2026-08-16",
        )

        self.assertEqual(first.accepted_rows, 1)
        self.assertEqual(second.accepted_rows, 1)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM keywords").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM keyword_sources").fetchone()[0],
            2,
        )
        snapshots = self.connection.execute(
            """
            SELECT metric_date, average_monthly_searches
            FROM keyword_metric_snapshots
            ORDER BY metric_date
            """
        ).fetchall()
        self.assertEqual(
            [tuple(snapshot) for snapshot in snapshots],
            [("2026-07-16", 1200), ("2026-08-16", 1500)],
        )

    def test_negative_and_parse_error_rows_are_rejected_and_audited(self) -> None:
        self.connection.execute(
            "UPDATE projects SET negative_terms_json = ? WHERE id = ?",
            ('["免费"]', self.project_id),
        )

        result = self.service.import_preview(
            self.project_id,
            self._preview(
                self._valid_row(keyword="免费 VPN"),
                {
                    "row_number": 3,
                    "keyword": "",
                    "errors": ["关键词为空"],
                },
            ),
            original_filename="invalid.csv",
            metric_date="2026-07-16",
        )

        self.assertEqual(result.accepted_rows, 0)
        self.assertEqual(result.rejected_rows, 2)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM keywords").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM keyword_sources").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM keyword_metric_snapshots").fetchone()[0],
            0,
        )

        rejected_rows = self.connection.execute(
            """
            SELECT row_number, keyword, status, rejection_reason
            FROM import_rows
            ORDER BY row_number
            """
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rejected_rows],
            [
                (2, "免费 VPN", "rejected", "negative_term: 免费"),
                (3, "", "rejected", "关键词为空"),
            ],
        )

    def test_import_for_missing_project_raises_value_error_without_writes(self) -> None:
        with self.assertRaisesRegex(ValueError, "project"):
            self.service.import_preview(
                99999,
                self._preview(self._valid_row()),
                original_filename="missing-project.csv",
                metric_date="2026-07-16",
            )

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0],
            0,
        )

    @staticmethod
    def _valid_row(
        *, keyword: str = "VPN 推荐", monthly_searches: int = 1200
    ) -> dict[str, object]:
        return {
            "row_number": 2,
            "keyword": keyword,
            "average_monthly_searches": monthly_searches,
            "competition_level": "MEDIUM",
            "competition_index": 45,
            "low_top_of_page_bid_micros": 1000000,
            "high_top_of_page_bid_micros": 2500000,
            "errors": [],
        }

    @staticmethod
    def _preview(*rows: dict[str, object]) -> dict[str, object]:
        return {"source_type": "file_import", "rows": list(rows)}

    def _create_schema(self) -> None:
        """Minimum migrated schema; import_rows is a required new migration."""
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE projects (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                default_country TEXT NOT NULL,
                default_language TEXT NOT NULL,
                negative_terms_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE keywords (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                keyword TEXT NOT NULL,
                normalized_keyword TEXT NOT NULL,
                country_code TEXT NOT NULL,
                language_code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                UNIQUE(project_id, normalized_keyword, country_code, language_code)
            );

            CREATE TABLE import_batches (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                original_filename TEXT NOT NULL,
                file_sha256 TEXT,
                metric_date TEXT NOT NULL,
                status TEXT NOT NULL,
                total_rows INTEGER NOT NULL,
                accepted_rows INTEGER NOT NULL,
                rejected_rows INTEGER NOT NULL
            );

            CREATE TABLE keyword_sources (
                id INTEGER PRIMARY KEY,
                keyword_id INTEGER NOT NULL REFERENCES keywords(id),
                source_type TEXT NOT NULL,
                import_batch_id INTEGER REFERENCES import_batches(id)
            );

            CREATE TABLE keyword_metric_snapshots (
                id INTEGER PRIMARY KEY,
                keyword_id INTEGER NOT NULL REFERENCES keywords(id),
                source_type TEXT NOT NULL,
                metric_date TEXT NOT NULL,
                country_code TEXT NOT NULL,
                language_code TEXT NOT NULL,
                average_monthly_searches INTEGER,
                competition_level TEXT,
                competition_index INTEGER,
                low_top_of_page_bid_micros INTEGER,
                high_top_of_page_bid_micros INTEGER
            );

            CREATE TABLE import_rows (
                id INTEGER PRIMARY KEY,
                import_batch_id INTEGER NOT NULL REFERENCES import_batches(id),
                row_number INTEGER NOT NULL,
                keyword TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                rejection_reason TEXT
            );
            """
        )


if __name__ == "__main__":
    unittest.main()
