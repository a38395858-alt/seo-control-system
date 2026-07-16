"""CSV parsing for Google Ads and generic keyword-list imports."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ImportedKeywordRecord:
    keyword: str
    avg_monthly_searches: int | None
    competition: str | None
    competition_index: int | None
    top_of_page_bid_low: float | None
    top_of_page_bid_high: float | None


@dataclass(frozen=True)
class ImportRowError:
    row_number: int
    message: str


@dataclass
class ImportPreview:
    records: list[ImportedKeywordRecord] = field(default_factory=list)
    errors: list[ImportRowError] = field(default_factory=list)


_HEADER_ALIASES = {
    "keyword": "keyword",
    "avg. monthly searches": "avg_monthly_searches",
    "search_volume": "avg_monthly_searches",
    "competition": "competition",
    "competition index": "competition_index",
    "top of page bid (low range)": "top_of_page_bid_low",
    "top of page bid (high range)": "top_of_page_bid_high",
    "cpc": "cpc",
}


def parse_keyword_csv(csv_text: str) -> ImportPreview:
    """Return a reviewed preview from Ads exports or a generic keyword CSV.

    The importer intentionally accepts extra columns so a customer export need
    not be edited before upload.  Only the normalized fields used by the
    keyword asset model are retained.
    """

    reader = csv.DictReader(io.StringIO(csv_text, newline=""))
    if reader.fieldnames is None:
        return ImportPreview()
    reader.fieldnames = [field.lstrip("\ufeff") if field else field for field in reader.fieldnames]
    preview = ImportPreview()
    for row_number, row in enumerate(reader, start=2):
        normalized_row = {
            _canonical_header(header): value
            for header, value in row.items()
            if header is not None
        }
        keyword = _clean_text(normalized_row.get("keyword"))
        if not keyword:
            preview.errors.append(ImportRowError(row_number, "Keyword is required."))
            continue
        preview.records.append(
            ImportedKeywordRecord(
                keyword=keyword,
                avg_monthly_searches=_parse_int(normalized_row.get("avg_monthly_searches")),
                competition=_clean_text(normalized_row.get("competition")),
                competition_index=_parse_int(normalized_row.get("competition_index")),
                top_of_page_bid_low=_parse_float(normalized_row.get("top_of_page_bid_low")),
                top_of_page_bid_high=_parse_float(normalized_row.get("top_of_page_bid_high")),
            )
        )
    return preview


def _canonical_header(header: str) -> str:
    text = str(header).strip().lstrip("\ufeff")
    return _HEADER_ALIASES.get(text.lower(), text)


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_int(value: object) -> int | None:
    text = _clean_text(value)
    return None if text is None else int(text.replace(",", ""))


def _parse_float(value: object) -> float | None:
    text = _clean_text(value)
    return None if text is None else float(text.replace(",", ""))
