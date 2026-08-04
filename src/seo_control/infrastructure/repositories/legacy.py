"""Remaining first-party, legacy research, schedule, and queue migration."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Protocol, Sequence

from seo_control.infrastructure.database import initialize_database

from .evidence import EvidenceTableSpec
from .migration import SnapshotMigrationService
from .projects import _timestamp


EXCLUDED_MIGRATION_TABLES = frozenset({"schema_migrations", "gsc_oauth_connection"})


def _spec(
    source_table: str,
    target_table: str,
    columns: Sequence[str],
    *,
    sql: str | None = None,
    timestamps: Sequence[str] = (),
    integers: Sequence[str] = (),
    floats: Sequence[str] = (),
    jsons: Sequence[str] = (),
    foreign_keys: Sequence[tuple[str, str, str]] = (),
) -> EvidenceTableSpec:
    return EvidenceTableSpec(
        source_table=source_table,
        target_table=target_table,
        columns=tuple(columns),
        key_columns=("id",),
        source_select_sql=sql or f"SELECT {','.join(columns)} FROM {source_table} ORDER BY id",
        timestamp_columns=tuple(timestamps),
        integer_columns=tuple(integers),
        float_columns=tuple(floats),
        json_columns=tuple(jsons),
        foreign_keys=tuple(foreign_keys),
    )


LEGACY_TABLE_SPECS: tuple[EvidenceTableSpec, ...] = (
    _spec(
        "project_knowledge_documents", "workspace_knowledge_documents",
        ("id", "project_id", "title", "source_type", "url", "content", "knowledge_type", "status", "created_at", "updated_at", "summary", "tags_json", "classification_json", "organizer_provider", "organizer_model"),
        timestamps=("created_at", "updated_at"), jsons=("tags_json", "classification_json"), integers=("id", "project_id"),
    ),
    _spec(
        "project_knowledge_crawl_runs", "workspace_knowledge_crawl_runs",
        ("id", "project_id", "status", "max_pages", "discovered_count", "accepted_count", "skipped_count", "failed_count", "message", "failure_reason", "created_at", "completed_at"),
        timestamps=("created_at", "completed_at"),
        integers=("id", "project_id", "max_pages", "discovered_count", "accepted_count", "skipped_count", "failed_count"),
    ),
    _spec(
        "project_knowledge_crawl_pages", "workspace_knowledge_crawl_pages",
        ("project_id", "id", "crawl_run_id", "url", "title", "knowledge_type", "status", "reason"),
        sql="SELECT runs.project_id AS project_id,pages.id,pages.crawl_run_id,pages.url,pages.title,pages.knowledge_type,pages.status,pages.reason FROM project_knowledge_crawl_pages AS pages JOIN project_knowledge_crawl_runs AS runs ON runs.id=pages.crawl_run_id ORDER BY pages.id",
        integers=("project_id", "id", "crawl_run_id"),
        foreign_keys=(("crawl_run_id", "workspace_knowledge_crawl_runs", "CASCADE"),),
    ),
    _spec(
        "authority_search_results", "workspace_authority_search_results",
        ("id", "search_run_id", "project_id", "content_asset_id", "section_heading", "claim_topic", "search_query", "rank", "title", "url", "domain", "status", "error_summary", "created_at"),
        timestamps=("created_at",), integers=("id", "project_id", "content_asset_id", "rank"),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"),),
    ),
    _spec(
        "authority_url_exclusions", "workspace_authority_url_exclusions",
        ("id", "project_id", "normalized_url", "reason", "first_seen_at", "last_seen_at"),
        timestamps=("first_seen_at", "last_seen_at"), integers=("id", "project_id"),
    ),
    _spec(
        "competitor_learning_schedules", "workspace_competitor_learning_schedules",
        ("id", "project_id", "topics_json", "interval_days", "enabled", "provider", "model", "last_run_at", "next_run_at", "created_at", "updated_at"),
        timestamps=("last_run_at", "next_run_at", "created_at", "updated_at"), jsons=("topics_json",),
        integers=("id", "project_id", "interval_days", "enabled"),
    ),
    _spec(
        "competitor_learning_runs", "workspace_legacy_competitor_learning_runs",
        ("id", "project_id", "schedule_id", "content_asset_id", "topic", "trigger_type", "status", "source_count", "cards_created", "error_summary", "started_at", "completed_at", "created_at"),
        timestamps=("started_at", "completed_at", "created_at"),
        integers=("id", "project_id", "schedule_id", "content_asset_id", "source_count", "cards_created"),
        foreign_keys=(("schedule_id", "workspace_competitor_learning_schedules", "SET NULL"), ("content_asset_id", "workspace_content_assets", "SET NULL")),
    ),
    _spec(
        "competitor_style_cards", "workspace_legacy_competitor_style_cards",
        ("id", "project_id", "schedule_id", "learning_run_id", "topic", "card_title", "summary", "evidence_json", "source_signature", "quality_score", "status", "created_at", "updated_at"),
        timestamps=("created_at", "updated_at"), jsons=("evidence_json",),
        integers=("id", "project_id", "schedule_id", "learning_run_id"), floats=("quality_score",),
        foreign_keys=(("schedule_id", "workspace_competitor_learning_schedules", "SET NULL"), ("learning_run_id", "workspace_legacy_competitor_learning_runs", "SET NULL")),
    ),
    _spec(
        "competitor_research_runs", "workspace_competitor_research_runs",
        ("id", "project_id", "content_asset_id", "query", "locale", "provider", "model", "status", "discovered_count", "usable_count", "analysis_json", "error_summary", "started_at", "completed_at"),
        timestamps=("started_at", "completed_at"), jsons=("analysis_json",),
        integers=("id", "project_id", "content_asset_id", "discovered_count", "usable_count"),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"),),
    ),
    _spec(
        "competitor_research_items", "workspace_competitor_research_items",
        ("project_id", "id", "research_run_id", "memory_id", "rank", "search_title", "url", "domain", "status", "error_summary", "created_at"),
        sql="SELECT runs.project_id AS project_id,items.id,items.research_run_id,items.memory_id,items.rank,items.search_title,items.url,items.domain,items.status,items.error_summary,items.created_at FROM competitor_research_items AS items JOIN competitor_research_runs AS runs ON runs.id=items.research_run_id ORDER BY items.id",
        timestamps=("created_at",), integers=("project_id", "id", "research_run_id", "memory_id", "rank"),
        foreign_keys=(("research_run_id", "workspace_competitor_research_runs", "CASCADE"), ("memory_id", "workspace_competitor_content", "SET NULL")),
    ),
    _spec(
        "competitor_url_archive", "workspace_competitor_url_archive",
        ("id", "project_id", "normalized_url", "url", "domain", "search_title", "status", "last_rank", "last_query", "error_summary", "discovered_count", "first_seen_at", "last_seen_at"),
        timestamps=("first_seen_at", "last_seen_at"),
        integers=("id", "project_id", "last_rank", "discovered_count"),
    ),
    _spec(
        "durable_task_queue", "workspace_durable_task_queue",
        ("id", "project_id", "task_type", "resource_id", "payload_json", "dedup_key", "status", "attempt_count", "max_attempts", "available_at", "lease_expires_at", "celery_task_id", "last_error", "created_at", "started_at", "completed_at", "updated_at"),
        timestamps=("available_at", "lease_expires_at", "created_at", "started_at", "completed_at", "updated_at"), jsons=("payload_json",),
        integers=("id", "project_id", "resource_id", "attempt_count", "max_attempts"),
    ),
)

_SPEC_BY_SOURCE = {spec.source_table: spec for spec in LEGACY_TABLE_SPECS}


class LegacyRepository(Protocol):
    def list_migration_snapshots(self) -> list[dict[str, Any]]: ...
    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None: ...


class SQLiteLegacyRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = database_path

    def list_migration_snapshots(self) -> list[dict[str, Any]]:
        connection = initialize_database(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            snapshots: list[dict[str, Any]] = []
            for spec in LEGACY_TABLE_SPECS:
                snapshots.extend(
                    {"source_table": spec.source_table, **dict(row)}
                    for row in connection.execute(spec.source_select_sql).fetchall()
                )
            return snapshots
        finally:
            connection.close()

    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        raise RuntimeError("SQLite is the source of this staged legacy-data migration")


class PostgresLegacyRepository:
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

    @staticmethod
    def _column_type(spec: EvidenceTableSpec, column: str) -> str:
        if column == "id":
            return "BIGSERIAL"
        if column in spec.integer_columns:
            return "BIGINT"
        if column in spec.float_columns:
            return "DOUBLE PRECISION"
        if column in spec.timestamp_columns:
            return "TIMESTAMPTZ"
        if column in spec.json_columns:
            return "JSONB"
        return "TEXT"

    @classmethod
    def _ddl(cls, spec: EvidenceTableSpec) -> str:
        definitions: list[str] = []
        for column in spec.columns:
            definition = f"{column} {cls._column_type(spec, column)}"
            if column == "project_id":
                definition += " NOT NULL REFERENCES workspace_projects(id) ON DELETE CASCADE"
            if column == "id":
                definition += " PRIMARY KEY"
            definitions.append(definition)
        for column, parent, on_delete in spec.foreign_keys:
            definitions.append(f"FOREIGN KEY({column}) REFERENCES {parent}(id) ON DELETE {on_delete}")
        definitions.append("migrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        return f"CREATE TABLE IF NOT EXISTS {spec.target_table} ({','.join(definitions)})"

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            for spec in LEGACY_TABLE_SPECS:
                cursor.execute(self._ddl(spec))
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{spec.target_table}_project ON {spec.target_table}(project_id)"
                )
        self._initialized = True

    def list_migration_snapshots(self) -> list[dict[str, Any]]:
        self.initialize()
        snapshots: list[dict[str, Any]] = []
        with self._connect() as connection, connection.cursor() as cursor:
            for spec in LEGACY_TABLE_SPECS:
                cursor.execute(f"SELECT {','.join(spec.columns)} FROM {spec.target_table} ORDER BY id")
                snapshots.extend(
                    {"source_table": spec.source_table, **dict(row)} for row in cursor.fetchall()
                )
        return snapshots

    @staticmethod
    def _snapshot_spec(snapshot: Mapping[str, Any]) -> EvidenceTableSpec:
        source_table = snapshot.get("source_table")
        if not isinstance(source_table, str) or source_table not in _SPEC_BY_SOURCE:
            raise ValueError("unknown legacy snapshot source_table")
        return _SPEC_BY_SOURCE[source_table]

    @staticmethod
    def _execute_upsert(cursor: Any, spec: EvidenceTableSpec, snapshot: Mapping[str, Any]) -> None:
        values = _legacy_values(spec, snapshot)
        updates = [column for column in spec.columns if column != "id"]
        placeholders = ["%s::jsonb" if column in spec.json_columns else "%s" for column in spec.columns]
        cursor.execute(
            f"INSERT INTO {spec.target_table}({','.join(spec.columns)},migrated_at) "
            f"VALUES({','.join(placeholders)},NOW()) ON CONFLICT(id) DO UPDATE SET "
            + ",".join(f"{column}=excluded.{column}" for column in updates)
            + ",migrated_at=NOW()",
            tuple(values[column] for column in spec.columns),
        )

    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            self._execute_upsert(cursor, self._snapshot_spec(snapshot), snapshot)

    def upsert_migration_snapshots(self, snapshots: Sequence[Mapping[str, Any]]) -> None:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            for snapshot in snapshots:
                self._execute_upsert(cursor, self._snapshot_spec(snapshot), snapshot)

    def reset_identity(self) -> None:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            for spec in LEGACY_TABLE_SPECS:
                cursor.execute(
                    f"SELECT setval(pg_get_serial_sequence('{spec.target_table}','id'),"
                    f"GREATEST(COALESCE(MAX(id),1),1),COALESCE(MAX(id),0)>0) FROM {spec.target_table}"
                )


def _canonical_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value.strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _legacy_values(spec: EvidenceTableSpec, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    values = {column: snapshot.get(column) for column in spec.columns}
    if values.get("project_id") is None or int(values["project_id"]) < 1:
        raise ValueError(f"{spec.source_table} snapshot project_id must be positive")
    for column in spec.integer_columns:
        if values[column] is not None:
            values[column] = int(values[column])
    for column in spec.float_columns:
        if values[column] is not None:
            values[column] = float(values[column])
    for column in spec.json_columns:
        if values[column] is not None:
            values[column] = _canonical_json(values[column])
    return values


def _comparable_legacy(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    spec = PostgresLegacyRepository._snapshot_spec(snapshot)
    values = _legacy_values(spec, snapshot)
    for column in spec.timestamp_columns:
        values[column] = _timestamp(values[column])
    return {"source_table": spec.source_table, **values}


def _snapshot_key(snapshot: Mapping[str, Any]) -> str:
    spec = PostgresLegacyRepository._snapshot_spec(snapshot)
    return f"{spec.source_table}:{int(snapshot['id'])}"


class LegacyMigrationService(SnapshotMigrationService):
    def __init__(self, source: LegacyRepository, target: LegacyRepository) -> None:
        super().__init__(source, target, key=_snapshot_key, normalize=_comparable_legacy)
