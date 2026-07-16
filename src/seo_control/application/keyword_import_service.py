"""Transactional persistence for reviewed keyword-import previews.

File parsing belongs to the importer/preview layer.  This service receives its
plain-data preview and persists the accepted rows as keyword assets, retaining
rejected rows as an audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import sqlite3
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ImportResult:
    """Summary returned after a preview has been committed."""

    import_batch_id: int
    total_rows: int
    accepted_rows: int
    rejected_rows: int


class KeywordImportService:
    """Write one reviewed keyword-import preview to a SQLite connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create_project(
        self, name: str, country_code: str, language_code: str
    ) -> int:
        """Create a project using its default keyword market."""
        values = {
            "name": name,
            "default_country": country_code,
            "default_language": language_code,
            "negative_terms_json": "[]",
        }
        cursor = self._insert_known_columns("projects", values)
        self.connection.commit()
        return int(cursor.lastrowid)

    def import_preview(
        self,
        project_id: int,
        preview: Mapping[str, Any],
        original_filename: str,
        metric_date: str | date,
    ) -> ImportResult:
        """Persist a file-import preview atomically.

        A normalized keyword is unique only within a project and its target
        market.  Re-importing it therefore adds provenance and a fresh metric
        snapshot rather than a second keyword asset.
        """
        project = self.connection.execute(
            """
            SELECT id, default_country, default_language, negative_terms_json
            FROM projects
            WHERE id = ?
            """,
            (project_id,),
        ).fetchone()
        if project is None:
            raise ValueError(f"project {project_id} does not exist")

        rows = preview.get("rows", [])
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ValueError("preview rows must be a sequence")

        source_type = str(preview.get("source_type") or "file_import")
        metric_date_value = (
            metric_date.isoformat() if isinstance(metric_date, date) else str(metric_date)
        )
        country_code = str(preview.get("country_code") or project["default_country"])
        language_code = str(
            preview.get("language_code") or project["default_language"]
        )
        negative_terms = self._negative_terms(project["negative_terms_json"])

        accepted_rows = 0
        rejected_rows = 0

        # A transaction makes a failed import all-or-nothing.  ``with`` also
        # works with an in-memory connection used by the unit tests.
        with self.connection:
            batch_cursor = self._insert_known_columns(
                "import_batches",
                {
                    "project_id": project_id,
                    "original_filename": original_filename,
                    # File bytes are intentionally not available at this
                    # boundary.  A stable fallback still gives the batch an
                    # audit value when the schema requires a hash.
                    "file_sha256": hashlib.sha256(
                        original_filename.encode("utf-8")
                    ).hexdigest(),
                    "metric_date": metric_date_value,
                    "status": "processing",
                    "total_rows": len(rows),
                    "accepted_rows": 0,
                    "rejected_rows": 0,
                },
            )
            batch_id = int(batch_cursor.lastrowid)

            for index, raw_row in enumerate(rows, start=1):
                if not isinstance(raw_row, Mapping):
                    rejected_rows += 1
                    self._record_rejection(
                        batch_id,
                        index,
                        "",
                        "row must be an object",
                    )
                    continue

                row_number = self._integer_or_default(raw_row.get("row_number"), index)
                raw_keyword = "" if raw_row.get("keyword") is None else str(raw_row.get("keyword"))
                keyword = self._clean_keyword(raw_keyword)
                errors = self._row_errors(raw_row.get("errors"))
                if errors:
                    rejected_rows += 1
                    self._record_rejection(batch_id, row_number, raw_keyword, errors[0])
                    continue
                if not keyword:
                    rejected_rows += 1
                    self._record_rejection(batch_id, row_number, raw_keyword, "keyword is empty")
                    continue

                negative_term = self._matching_negative_term(keyword, negative_terms)
                if negative_term is not None:
                    rejected_rows += 1
                    self._record_rejection(
                        batch_id,
                        row_number,
                        raw_keyword,
                        f"negative_term: {negative_term}",
                    )
                    continue

                normalized_keyword = self._normalize_keyword(keyword)
                keyword_id = self._find_keyword(
                    project_id, normalized_keyword, country_code, language_code
                )
                if keyword_id is None:
                    keyword_cursor = self._insert_known_columns(
                        "keywords",
                        {
                            "project_id": project_id,
                            "keyword": keyword,
                            "normalized_keyword": normalized_keyword,
                            "country_code": country_code,
                            "language_code": language_code,
                            "status": "active",
                        },
                    )
                    keyword_id = int(keyword_cursor.lastrowid)

                self._insert_known_columns(
                    "keyword_sources",
                    {
                        "keyword_id": keyword_id,
                        "source_type": source_type,
                        "import_batch_id": batch_id,
                    },
                )
                self._insert_known_columns(
                    "keyword_metric_snapshots",
                    {
                        "keyword_id": keyword_id,
                        "source_type": source_type,
                        "metric_date": metric_date_value,
                        "country_code": country_code,
                        "language_code": language_code,
                        "average_monthly_searches": self._optional_integer(
                            raw_row.get("average_monthly_searches")
                        ),
                        "competition_level": raw_row.get("competition_level"),
                        "competition_index": self._optional_integer(
                            raw_row.get("competition_index")
                        ),
                        "low_top_of_page_bid_micros": self._optional_integer(
                            raw_row.get("low_top_of_page_bid_micros")
                        ),
                        "high_top_of_page_bid_micros": self._optional_integer(
                            raw_row.get("high_top_of_page_bid_micros")
                        ),
                    },
                )
                accepted_rows += 1

            self.connection.execute(
                """
                UPDATE import_batches
                SET status = ?, accepted_rows = ?, rejected_rows = ?
                WHERE id = ?
                """,
                ("completed", accepted_rows, rejected_rows, batch_id),
            )

        return ImportResult(
            import_batch_id=batch_id,
            total_rows=len(rows),
            accepted_rows=accepted_rows,
            rejected_rows=rejected_rows,
        )

    def _find_keyword(
        self,
        project_id: int,
        normalized_keyword: str,
        country_code: str,
        language_code: str,
    ) -> int | None:
        row = self.connection.execute(
            """
            SELECT id
            FROM keywords
            WHERE project_id = ?
              AND normalized_keyword = ?
              AND country_code = ?
              AND language_code = ?
            """,
            (project_id, normalized_keyword, country_code, language_code),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def _record_rejection(
        self, batch_id: int, row_number: int, keyword: str, reason: str
    ) -> None:
        self._insert_known_columns(
            "import_rows",
            {
                "import_batch_id": batch_id,
                "row_number": row_number,
                "keyword": keyword,
                "status": "rejected",
                "rejection_reason": reason,
            },
        )

    def _insert_known_columns(
        self, table: str, values: Mapping[str, Any]
    ) -> sqlite3.Cursor:
        """Insert only fields exposed by the active migration version.

        The product's old local databases and its current schema have a few
        optional audit fields.  Keeping this boundary column-aware avoids
        coupling imports to those optional fields while preserving all fields
        that do exist.
        """
        columns = self._table_columns(table)
        insert_values = {key: value for key, value in values.items() if key in columns}
        if not insert_values:
            raise ValueError(f"table {table} has no compatible import columns")
        quoted_columns = ", ".join(insert_values)
        placeholders = ", ".join("?" for _ in insert_values)
        return self.connection.execute(
            f"INSERT INTO {table} ({quoted_columns}) VALUES ({placeholders})",
            tuple(insert_values.values()),
        )

    def _table_columns(self, table: str) -> set[str]:
        allowed_tables = {
            "projects",
            "keywords",
            "import_batches",
            "import_rows",
            "keyword_sources",
            "keyword_metric_snapshots",
        }
        if table not in allowed_tables:
            raise ValueError(f"unsupported table: {table}")
        return {str(row[1]) for row in self.connection.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _clean_keyword(value: str) -> str:
        return " ".join(value.split())

    @classmethod
    def _normalize_keyword(cls, value: str) -> str:
        return cls._clean_keyword(value).casefold()

    @staticmethod
    def _row_errors(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, Sequence):
            return [str(error) for error in value if str(error).strip()]
        return [str(value)]

    @classmethod
    def _negative_terms(cls, raw_value: Any) -> list[str]:
        try:
            parsed = json.loads(raw_value or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(parsed, list):
            return []
        return [cls._clean_keyword(str(item)) for item in parsed if str(item).strip()]

    @classmethod
    def _matching_negative_term(
        cls, keyword: str, negative_terms: Sequence[str]
    ) -> str | None:
        normalized_keyword = cls._normalize_keyword(keyword)
        for term in negative_terms:
            if cls._normalize_keyword(term) in normalized_keyword:
                return term
        return None

    @staticmethod
    def _optional_integer(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _integer_or_default(value: Any, default: int) -> int:
        converted = KeywordImportService._optional_integer(value)
        return default if converted is None else converted
