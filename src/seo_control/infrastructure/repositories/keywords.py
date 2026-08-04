"""Project-scoped keyword repositories and staged PostgreSQL migration."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Protocol, Sequence

from seo_control.infrastructure.database import initialize_database

from .migration import SnapshotMigrationService
from .projects import _timestamp


KEYWORD_BASE_COLUMNS = (
    "id", "project_id", "keyword", "normalized_keyword", "country_code", "language_code",
    "status", "priority", "opportunity_score", "demand_estimate", "first_discovered_at",
    "last_seen_at", "created_at", "updated_at", "deleted_at",
)
KEYWORD_DERIVED_COLUMNS = (
    "search_volume", "competition_level", "competition_index", "metric_date", "category",
    "search_intent", "is_seo_content_fit", "selected_title", "title_candidate_count",
)
KEYWORD_SNAPSHOT_COLUMNS = KEYWORD_BASE_COLUMNS + KEYWORD_DERIVED_COLUMNS
KEYWORD_API_COLUMNS = (
    "id", "keyword", "country_code", "language_code", "demand_estimate", "search_volume",
    "competition_level", "competition_index", "metric_date", "category", "search_intent",
    "is_seo_content_fit", "selected_title", "title_candidate_count",
)
KEYWORD_TIMESTAMP_COLUMNS = (
    "first_discovered_at", "last_seen_at", "created_at", "updated_at", "deleted_at",
)


class KeywordRepository(Protocol):
    def list_keywords(self, project_id: int) -> list[dict[str, Any]]: ...
    def soft_delete(self, project_id: int, *, keyword_ids: Sequence[int] = (), clear_all: bool = False) -> int: ...
    def list_migration_snapshots(self) -> list[dict[str, Any]]: ...
    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None: ...


def _keyword_select(where: str, order_by: str) -> str:
    return f"""SELECT keywords.id,keywords.project_id,keywords.keyword,keywords.normalized_keyword,
               keywords.country_code,keywords.language_code,keywords.status,keywords.priority,
               keywords.opportunity_score,keywords.demand_estimate,keywords.first_discovered_at,
               keywords.last_seen_at,keywords.created_at,keywords.updated_at,keywords.deleted_at,
               metrics.average_monthly_searches AS search_volume,metrics.competition_level,
               metrics.competition_index,metrics.metric_date,
               (SELECT categories.name FROM keyword_category_assignments AS assignments
                  JOIN keyword_categories AS categories ON categories.id=assignments.category_id
                  WHERE assignments.keyword_id=keywords.id ORDER BY assignments.created_at DESC LIMIT 1) AS category,
               (SELECT reviews.search_intent FROM keyword_reviews AS reviews
                  WHERE reviews.keyword_id=keywords.id ORDER BY reviews.id DESC LIMIT 1) AS search_intent,
               (SELECT reviews.is_seo_content_fit FROM keyword_reviews AS reviews
                  WHERE reviews.keyword_id=keywords.id ORDER BY reviews.id DESC LIMIT 1) AS is_seo_content_fit,
               (SELECT candidates.title FROM keyword_title_candidates AS candidates
                  WHERE candidates.keyword_id=keywords.id AND candidates.status='selected'
                    AND candidates.deleted_at IS NULL LIMIT 1) AS selected_title,
               (SELECT COUNT(*) FROM keyword_title_candidates AS candidates
                  WHERE candidates.keyword_id=keywords.id AND candidates.deleted_at IS NULL) AS title_candidate_count
               FROM keywords
               LEFT JOIN keyword_metric_snapshots AS metrics ON metrics.id=(
                 SELECT latest.id FROM keyword_metric_snapshots AS latest WHERE latest.keyword_id=keywords.id
                 ORDER BY latest.metric_date DESC,latest.id DESC LIMIT 1)
               WHERE {where} ORDER BY {order_by}"""


class SQLiteKeywordRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = database_path

    def _connection(self) -> sqlite3.Connection:
        connection = initialize_database(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def list_keywords(self, project_id: int) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            rows = connection.execute(
                _keyword_select(
                    "keywords.project_id=? AND keywords.deleted_at IS NULL",
                    "keywords.keyword COLLATE NOCASE,keywords.id",
                ),
                (project_id,),
            ).fetchall()
            return [{column: row[column] for column in KEYWORD_API_COLUMNS} for row in rows]
        finally:
            connection.close()

    def soft_delete(self, project_id: int, *, keyword_ids: Sequence[int] = (), clear_all: bool = False) -> int:
        ids = tuple(dict.fromkeys(int(value) for value in keyword_ids))
        if not clear_all and not ids:
            raise ValueError("keyword_ids are required unless clear_all is true")
        connection = self._connection()
        try:
            with connection:
                if clear_all:
                    cursor = connection.execute(
                        "UPDATE keywords SET deleted_at=CURRENT_TIMESTAMP WHERE project_id=? AND deleted_at IS NULL",
                        (project_id,),
                    )
                else:
                    marks = ",".join("?" for _value in ids)
                    cursor = connection.execute(
                        f"UPDATE keywords SET deleted_at=CURRENT_TIMESTAMP WHERE project_id=? AND deleted_at IS NULL AND id IN ({marks})",
                        (project_id, *ids),
                    )
                return int(cursor.rowcount)
        finally:
            connection.close()

    def list_migration_snapshots(self) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            rows = connection.execute(_keyword_select("1=1", "keywords.id")).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        raise RuntimeError("SQLite is the source of this staged keyword migration")


class PostgresKeywordRepository:
    def __init__(self, database_url: str, *, connection_factory: Callable[..., Any] | None = None) -> None:
        if not database_url.strip():
            raise ValueError("PostgreSQL database URL is required")
        self.database_url = database_url
        self.connection_factory = connection_factory
        self._initialized = False

    def _connect(self):
        if self.connection_factory is not None:
            return self.connection_factory(self.database_url)
        try:
            from psycopg import connect
            from psycopg.rows import dict_row
        except ImportError as error:  # pragma: no cover - production dependency
            raise RuntimeError("install requirements-postgres.txt before PostgreSQL migration") from error
        return connect(self.database_url, row_factory=dict_row)

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS workspace_keywords (
                    id BIGSERIAL PRIMARY KEY,
                    project_id BIGINT NOT NULL REFERENCES workspace_projects(id) ON DELETE CASCADE,
                    keyword TEXT NOT NULL,normalized_keyword TEXT NOT NULL,country_code TEXT NOT NULL,
                    language_code TEXT NOT NULL,status TEXT NOT NULL,priority BIGINT NOT NULL DEFAULT 0,
                    opportunity_score DOUBLE PRECISION,demand_estimate BIGINT,
                    first_discovered_at TIMESTAMPTZ NOT NULL,last_seen_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL,deleted_at TIMESTAMPTZ,
                    search_volume BIGINT,competition_level TEXT,competition_index BIGINT,metric_date DATE,
                    category TEXT,search_intent TEXT,is_seo_content_fit SMALLINT,selected_title TEXT,
                    title_candidate_count BIGINT NOT NULL DEFAULT 0,migrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(project_id,normalized_keyword,country_code,language_code)
                )"""
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_workspace_keywords_project ON workspace_keywords(project_id,deleted_at,id)"
            )
        self._initialized = True

    @staticmethod
    def _rows(cursor: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in cursor.fetchall()]

    def list_keywords(self, project_id: int) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {','.join(KEYWORD_API_COLUMNS)} FROM workspace_keywords "
                "WHERE project_id=%s AND deleted_at IS NULL ORDER BY LOWER(keyword),id",
                (project_id,),
            )
            return self._rows(cursor)

    def soft_delete(self, project_id: int, *, keyword_ids: Sequence[int] = (), clear_all: bool = False) -> int:
        ids = tuple(dict.fromkeys(int(value) for value in keyword_ids))
        if not clear_all and not ids:
            raise ValueError("keyword_ids are required unless clear_all is true")
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            if clear_all:
                cursor.execute(
                    "UPDATE workspace_keywords SET deleted_at=NOW() WHERE project_id=%s AND deleted_at IS NULL",
                    (project_id,),
                )
            else:
                cursor.execute(
                    "UPDATE workspace_keywords SET deleted_at=NOW() WHERE project_id=%s AND deleted_at IS NULL AND id=ANY(%s)",
                    (project_id, list(ids)),
                )
            return int(cursor.rowcount)

    def list_migration_snapshots(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT {','.join(KEYWORD_SNAPSHOT_COLUMNS)} FROM workspace_keywords ORDER BY id")
            return self._rows(cursor)

    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        values = _keyword_values(snapshot)
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO workspace_keywords(
                    id,project_id,keyword,normalized_keyword,country_code,language_code,status,priority,
                    opportunity_score,demand_estimate,first_discovered_at,last_seen_at,created_at,updated_at,
                    deleted_at,search_volume,competition_level,competition_index,metric_date,category,
                    search_intent,is_seo_content_fit,selected_title,title_candidate_count,migrated_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id,keyword=excluded.keyword,
                    normalized_keyword=excluded.normalized_keyword,country_code=excluded.country_code,
                    language_code=excluded.language_code,status=excluded.status,priority=excluded.priority,
                    opportunity_score=excluded.opportunity_score,demand_estimate=excluded.demand_estimate,
                    first_discovered_at=excluded.first_discovered_at,last_seen_at=excluded.last_seen_at,
                    created_at=excluded.created_at,updated_at=excluded.updated_at,deleted_at=excluded.deleted_at,
                    search_volume=excluded.search_volume,competition_level=excluded.competition_level,
                    competition_index=excluded.competition_index,metric_date=excluded.metric_date,
                    category=excluded.category,search_intent=excluded.search_intent,
                    is_seo_content_fit=excluded.is_seo_content_fit,selected_title=excluded.selected_title,
                    title_candidate_count=excluded.title_candidate_count,migrated_at=NOW()""",
                tuple(values[column] for column in KEYWORD_SNAPSHOT_COLUMNS),
            )

    def reset_identity(self) -> None:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT setval(pg_get_serial_sequence('workspace_keywords','id'),
                   GREATEST(COALESCE(MAX(id),1),1),COALESCE(MAX(id),0)>0) FROM workspace_keywords"""
            )


def _keyword_values(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "status": "pending_review", "priority": 0, "opportunity_score": None,
        "demand_estimate": None, "deleted_at": None, "search_volume": None,
        "competition_level": None, "competition_index": None, "metric_date": None,
        "category": None, "search_intent": None, "is_seo_content_fit": None,
        "selected_title": None, "title_candidate_count": 0,
    }
    values = {column: snapshot.get(column, defaults.get(column)) for column in KEYWORD_SNAPSHOT_COLUMNS}
    if not isinstance(values["id"], int) or int(values["id"]) < 1:
        raise ValueError("keyword snapshot id must be positive")
    if not isinstance(values["project_id"], int) or int(values["project_id"]) < 1:
        raise ValueError("keyword snapshot project_id must be positive")
    if not isinstance(values["keyword"], str) or not values["keyword"].strip():
        raise ValueError("keyword snapshot keyword is required")
    return values


def _comparable_keyword(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    values = _keyword_values(snapshot)
    for column in KEYWORD_TIMESTAMP_COLUMNS:
        values[column] = _timestamp(values[column])
    for column in ("id", "project_id", "priority", "title_candidate_count"):
        values[column] = int(values[column] or 0)
    for column in ("demand_estimate", "search_volume", "competition_index", "is_seo_content_fit"):
        values[column] = None if values[column] is None else int(values[column])
    values["opportunity_score"] = None if values["opportunity_score"] is None else float(values["opportunity_score"])
    values["metric_date"] = str(values["metric_date"] or "")
    return values


class KeywordMigrationService(SnapshotMigrationService):
    def __init__(self, source: KeywordRepository, target: KeywordRepository) -> None:
        super().__init__(
            source,
            target,
            key=lambda row: str(int(row["id"])),
            normalize=_comparable_keyword,
        )
