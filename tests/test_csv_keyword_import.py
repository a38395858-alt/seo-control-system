"""Contract tests for importing Google Ads keyword-plan CSV exports."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.csv_keyword_import import parse_keyword_csv


GOOGLE_ADS_HEADER = (
    "Keyword,Avg. monthly searches,Competition,Competition index,"
    "Top of page bid (low range),Top of page bid (high range)"
)


class CsvKeywordImportTests(unittest.TestCase):
    def test_parses_google_ads_english_headers_into_standard_keyword_records(self) -> None:
        preview = parse_keyword_csv(
            "\n".join(
                (
                    GOOGLE_ADS_HEADER,
                    '"seo tools","1,200",HIGH,87,1.25,6.50',
                )
            )
        )

        self.assertEqual([], preview.errors)
        self.assertEqual(1, len(preview.records))
        record = preview.records[0]
        self.assertEqual("seo tools", record.keyword)
        self.assertEqual(1200, record.avg_monthly_searches)
        self.assertEqual("HIGH", record.competition)
        self.assertEqual(87, record.competition_index)
        self.assertEqual(1.25, record.top_of_page_bid_low)
        self.assertEqual(6.50, record.top_of_page_bid_high)

    def test_reports_a_row_level_error_when_keyword_is_empty(self) -> None:
        preview = parse_keyword_csv(
            "\n".join(
                (
                    GOOGLE_ADS_HEADER,
                    ',500,MEDIUM,42,0.80,3.20',
                )
            )
        )

        self.assertEqual([], preview.records)
        self.assertEqual(1, len(preview.errors))
        self.assertEqual(2, preview.errors[0].row_number)
        self.assertIn("keyword", preview.errors[0].message.lower())

    def test_accepts_a_utf8_bom_before_the_google_ads_header(self) -> None:
        preview = parse_keyword_csv(
            "\n".join(
                (
                    "\ufeff" + GOOGLE_ADS_HEADER,
                    "content marketing,3200,LOW,15,0.50,2.75",
                )
            )
        )

        self.assertEqual([], preview.errors)
        self.assertEqual(1, len(preview.records))
        self.assertEqual("content marketing", preview.records[0].keyword)
        self.assertEqual(3200, preview.records[0].avg_monthly_searches)

    def test_ignores_unknown_columns_without_rejecting_the_row(self) -> None:
        header = GOOGLE_ADS_HEADER + ",Custom note"
        preview = parse_keyword_csv(
            "\n".join(
                (
                    header,
                    "technical seo,800,LOW,10,0.25,1.20,keep this column",
                )
            )
        )

        self.assertEqual([], preview.errors)
        self.assertEqual(1, len(preview.records))
        self.assertEqual("technical seo", preview.records[0].keyword)
        self.assertEqual(800, preview.records[0].avg_monthly_searches)


if __name__ == "__main__":
    unittest.main()
