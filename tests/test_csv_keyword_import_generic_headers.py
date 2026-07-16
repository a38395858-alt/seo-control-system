"""Contract tests for generic keyword CSV headers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.csv_keyword_import import parse_keyword_csv


class GenericCsvKeywordImportTests(unittest.TestCase):
    def test_parses_generic_lowercase_headers_and_ignores_cpc_and_extra_columns(self) -> None:
        preview = parse_keyword_csv(
            "\n".join(
                (
                    "keyword,search_volume,competition,cpc,import_batch",
                    '"local seo","2,400",LOW,1.75,july-campaign',
                )
            )
        )

        self.assertEqual([], preview.errors)
        self.assertEqual(1, len(preview.records))
        record = preview.records[0]
        self.assertEqual("local seo", record.keyword)
        self.assertEqual(2400, record.avg_monthly_searches)
        self.assertEqual("LOW", record.competition)


if __name__ == "__main__":
    unittest.main()
