"""Auditable keyword, collection, and learning-evidence migration repositories."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Protocol, Sequence

from seo_control.infrastructure.database import initialize_database

from .migration import SnapshotMigrationService
from .projects import _timestamp


@dataclass(frozen=True)
class EvidenceTableSpec:
    source_table: str
    target_table: str
    columns: tuple[str, ...]
    key_columns: tuple[str, ...]
    source_select_sql: str
    timestamp_columns: tuple[str, ...] = ()
    integer_columns: tuple[str, ...] = ()
    float_columns: tuple[str, ...] = ()
    json_columns: tuple[str, ...] = ()
    date_columns: tuple[str, ...] = ()
    foreign_keys: tuple[tuple[str, str, str], ...] = ()


def _direct(table: str, columns: Sequence[str]) -> str:
    return f"SELECT {','.join(columns)} FROM {table} ORDER BY "


def _spec(
    source_table: str,
    target_table: str,
    columns: Sequence[str],
    *,
    keys: Sequence[str] = ("id",),
    sql: str | None = None,
    timestamps: Sequence[str] = (),
    integers: Sequence[str] = (),
    floats: Sequence[str] = (),
    jsons: Sequence[str] = (),
    dates: Sequence[str] = (),
    foreign_keys: Sequence[tuple[str, str, str]] = (),
) -> EvidenceTableSpec:
    key_columns = tuple(keys)
    order = ",".join(key_columns)
    return EvidenceTableSpec(
        source_table=source_table,
        target_table=target_table,
        columns=tuple(columns),
        key_columns=key_columns,
        source_select_sql=sql or (_direct(source_table, columns) + order),
        timestamp_columns=tuple(timestamps),
        integer_columns=tuple(integers),
        float_columns=tuple(floats),
        json_columns=tuple(jsons),
        date_columns=tuple(dates),
        foreign_keys=tuple(foreign_keys),
    )


EVIDENCE_TABLE_SPECS: tuple[EvidenceTableSpec, ...] = (
    _spec(
        "keyword_research_tasks", "workspace_keyword_research_tasks",
        ("id", "project_id", "source_type", "mode", "country_code", "language_code", "parameters_json", "status", "discovered_count", "accepted_count", "rejected_count", "failure_reason", "started_at", "finished_at", "created_at", "updated_at"),
        timestamps=("started_at", "finished_at", "created_at", "updated_at"),
        integers=("id", "project_id", "discovered_count", "accepted_count", "rejected_count"),
        jsons=("parameters_json",),
    ),
    _spec(
        "import_batches", "workspace_import_batches",
        ("id", "project_id", "task_id", "original_filename", "file_sha256", "mapping_json", "metric_date", "status", "total_rows", "accepted_rows", "rejected_rows", "error_file_reference", "created_at", "completed_at"),
        timestamps=("created_at", "completed_at"), dates=("metric_date",),
        integers=("id", "project_id", "task_id", "total_rows", "accepted_rows", "rejected_rows"),
        jsons=("mapping_json",),
        foreign_keys=(("task_id", "workspace_keyword_research_tasks", "SET NULL"),),
    ),
    _spec(
        "import_rows", "workspace_import_rows",
        ("project_id", "id", "import_batch_id", "row_number", "keyword", "status", "rejection_reason", "created_at"),
        sql="SELECT batches.project_id AS project_id,rows.id,rows.import_batch_id,rows.row_number,rows.keyword,rows.status,rows.rejection_reason,rows.created_at FROM import_rows AS rows JOIN import_batches AS batches ON batches.id=rows.import_batch_id ORDER BY rows.id",
        timestamps=("created_at",), integers=("project_id", "id", "import_batch_id", "row_number"),
        foreign_keys=(("import_batch_id", "workspace_import_batches", "CASCADE"),),
    ),
    _spec(
        "suggest_query_jobs", "workspace_suggest_query_jobs",
        ("project_id", "id", "task_id", "query", "normalized_query", "hl", "gl", "expansion_rule", "expansion_depth", "protocol_version", "status", "http_status", "response_time_ms", "raw_response_reference", "error_code", "attempted_at", "created_at"),
        sql="SELECT tasks.project_id AS project_id,jobs.id,jobs.task_id,jobs.query,jobs.normalized_query,jobs.hl,jobs.gl,jobs.expansion_rule,jobs.expansion_depth,jobs.protocol_version,jobs.status,jobs.http_status,jobs.response_time_ms,jobs.raw_response_reference,jobs.error_code,jobs.attempted_at,jobs.created_at FROM suggest_query_jobs AS jobs JOIN keyword_research_tasks AS tasks ON tasks.id=jobs.task_id ORDER BY jobs.id",
        timestamps=("attempted_at", "created_at"), integers=("project_id", "id", "task_id", "expansion_depth", "http_status", "response_time_ms"),
        foreign_keys=(("task_id", "workspace_keyword_research_tasks", "CASCADE"),),
    ),
    _spec(
        "keyword_categories", "workspace_keyword_categories",
        ("id", "project_id", "name", "normalized_name", "created_at"),
        timestamps=("created_at",), integers=("id", "project_id"),
    ),
    _spec(
        "keyword_category_assignments", "workspace_keyword_category_assignments",
        ("project_id", "keyword_id", "category_id", "source", "confidence", "created_at"),
        keys=("keyword_id", "category_id"),
        sql="SELECT keywords.project_id AS project_id,assignments.keyword_id,assignments.category_id,assignments.source,assignments.confidence,assignments.created_at FROM keyword_category_assignments AS assignments JOIN keywords ON keywords.id=assignments.keyword_id ORDER BY assignments.keyword_id,assignments.category_id",
        timestamps=("created_at",), integers=("project_id", "keyword_id", "category_id"), floats=("confidence",),
        foreign_keys=(("keyword_id", "workspace_keywords", "CASCADE"), ("category_id", "workspace_keyword_categories", "CASCADE")),
    ),
    _spec(
        "keyword_reviews", "workspace_keyword_reviews",
        ("project_id", "id", "keyword_id", "seed_keyword", "provider", "is_seo_content_fit", "same_topic_as_seed", "search_intent", "recommended_action", "reason", "confidence", "created_at"),
        sql="SELECT keywords.project_id AS project_id,reviews.id,reviews.keyword_id,reviews.seed_keyword,reviews.provider,reviews.is_seo_content_fit,reviews.same_topic_as_seed,reviews.search_intent,reviews.recommended_action,reviews.reason,reviews.confidence,reviews.created_at FROM keyword_reviews AS reviews JOIN keywords ON keywords.id=reviews.keyword_id ORDER BY reviews.id",
        timestamps=("created_at",), integers=("project_id", "id", "keyword_id", "is_seo_content_fit", "same_topic_as_seed"), floats=("confidence",),
        foreign_keys=(("keyword_id", "workspace_keywords", "CASCADE"),),
    ),
    _spec(
        "keyword_sources", "workspace_keyword_sources",
        ("project_id", "id", "keyword_id", "source_type", "task_id", "import_batch_id", "seed_keyword", "parent_query", "expansion_rule", "expansion_depth", "source_position", "raw_record_reference", "discovered_at", "created_at"),
        sql="SELECT keywords.project_id AS project_id,sources.id,sources.keyword_id,sources.source_type,sources.task_id,sources.import_batch_id,sources.seed_keyword,sources.parent_query,sources.expansion_rule,sources.expansion_depth,sources.source_position,sources.raw_record_reference,sources.discovered_at,sources.created_at FROM keyword_sources AS sources JOIN keywords ON keywords.id=sources.keyword_id ORDER BY sources.id",
        timestamps=("discovered_at", "created_at"), integers=("project_id", "id", "keyword_id", "task_id", "import_batch_id", "expansion_depth", "source_position"),
        foreign_keys=(("keyword_id", "workspace_keywords", "CASCADE"), ("task_id", "workspace_keyword_research_tasks", "SET NULL"), ("import_batch_id", "workspace_import_batches", "SET NULL")),
    ),
    _spec(
        "keyword_metric_snapshots", "workspace_keyword_metric_snapshots",
        ("project_id", "id", "keyword_id", "source_type", "metric_date", "country_code", "language_code", "geo_set_hash", "average_monthly_searches", "monthly_search_volumes_json", "competition_level", "competition_index", "low_top_of_page_bid_micros", "high_top_of_page_bid_micros", "currency_code", "raw_record_reference", "created_at"),
        sql="SELECT keywords.project_id AS project_id,metrics.id,metrics.keyword_id,metrics.source_type,metrics.metric_date,metrics.country_code,metrics.language_code,metrics.geo_set_hash,metrics.average_monthly_searches,metrics.monthly_search_volumes_json,metrics.competition_level,metrics.competition_index,metrics.low_top_of_page_bid_micros,metrics.high_top_of_page_bid_micros,metrics.currency_code,metrics.raw_record_reference,metrics.created_at FROM keyword_metric_snapshots AS metrics JOIN keywords ON keywords.id=metrics.keyword_id ORDER BY metrics.id",
        timestamps=("created_at",), dates=("metric_date",), jsons=("monthly_search_volumes_json",),
        integers=("project_id", "id", "keyword_id", "average_monthly_searches", "competition_index", "low_top_of_page_bid_micros", "high_top_of_page_bid_micros"),
        foreign_keys=(("keyword_id", "workspace_keywords", "CASCADE"),),
    ),
    _spec(
        "competitor_catalog_collection_runs", "workspace_collection_runs",
        ("id", "project_id", "status", "candidate_ids_json", "total_count", "collected_count", "already_collected_count", "robots_blocked_count", "failed_count", "error_summary", "started_at", "completed_at", "created_at", "updated_at", "unchanged_count", "recovered_count", "last_heartbeat_at"),
        timestamps=("started_at", "completed_at", "created_at", "updated_at", "last_heartbeat_at"), jsons=("candidate_ids_json",),
        integers=("id", "project_id", "total_count", "collected_count", "already_collected_count", "robots_blocked_count", "failed_count", "unchanged_count", "recovered_count"),
    ),
    _spec(
        "competitor_content_memory", "workspace_competitor_content",
        ("id", "project_id", "normalized_url", "url", "domain", "page_title", "content", "content_hash", "structure_json", "first_captured_at", "last_captured_at"),
        timestamps=("first_captured_at", "last_captured_at"), jsons=("structure_json",), integers=("id", "project_id"),
    ),
    _spec(
        "competitor_content_chunks", "workspace_competitor_content_chunks",
        ("id", "project_id", "memory_id", "position", "content"), integers=("id", "project_id", "memory_id", "position"),
        foreign_keys=(("memory_id", "workspace_competitor_content", "CASCADE"),),
    ),
    _spec(
        "competitor_content_versions", "workspace_competitor_content_versions",
        ("id", "project_id", "memory_id", "content_hash", "content", "structure_json", "extractor", "captured_at"),
        timestamps=("captured_at",), jsons=("structure_json",), integers=("id", "project_id", "memory_id"),
        foreign_keys=(("memory_id", "workspace_competitor_content", "CASCADE"),),
    ),
    _spec(
        "collection_run_items", "workspace_collection_run_items",
        ("id", "project_id", "run_id", "catalog_id", "normalized_url", "source_url", "status", "attempt_count", "max_attempts", "memory_id", "version_id", "extractor", "error_summary", "started_at", "completed_at", "created_at", "updated_at"),
        timestamps=("started_at", "completed_at", "created_at", "updated_at"),
        integers=("id", "project_id", "run_id", "catalog_id", "attempt_count", "max_attempts", "memory_id", "version_id"),
        foreign_keys=(("run_id", "workspace_collection_runs", "CASCADE"), ("catalog_id", "workspace_competitor_urls", "SET NULL"), ("memory_id", "workspace_competitor_content", "SET NULL"), ("version_id", "workspace_competitor_content_versions", "SET NULL")),
    ),
    _spec(
        "competitor_content_learning_runs", "workspace_competitor_learning_runs",
        ("id", "project_id", "status", "candidate_memory_ids_json", "source_count", "processed_count", "memories_created_count", "provider", "model", "error_summary", "started_at", "completed_at", "created_at", "updated_at", "memories_updated_count", "memories_rejected_count"),
        timestamps=("started_at", "completed_at", "created_at", "updated_at"), jsons=("candidate_memory_ids_json",),
        integers=("id", "project_id", "source_count", "processed_count", "memories_created_count", "memories_updated_count", "memories_rejected_count"),
    ),
    _spec(
        "content_learning_memories", "workspace_learning_memories",
        ("id", "project_id", "memory_type", "topic", "summary", "evidence_json", "source_url", "source_content_hash", "quality_score", "status", "created_at", "updated_at", "manual_priority", "pinned", "positive_feedback_count", "negative_feedback_count", "last_feedback_at", "card_type", "confidence_score", "freshness_status", "applicability_json", "evidence_count", "last_validated_at", "superseded_by_id", "inference_level"),
        timestamps=("created_at", "updated_at", "last_feedback_at", "last_validated_at"), jsons=("evidence_json", "applicability_json"),
        integers=("id", "project_id", "manual_priority", "pinned", "positive_feedback_count", "negative_feedback_count", "evidence_count", "superseded_by_id"),
        floats=("quality_score", "confidence_score"),
    ),
    _spec(
        "content_learning_memory_sources", "workspace_learning_memory_sources",
        ("id", "project_id", "memory_id", "source_type", "source_id", "source_url", "source_content_hash", "source_version_id", "evidence_excerpt", "captured_at", "created_at"),
        timestamps=("captured_at", "created_at"), integers=("id", "project_id", "memory_id", "source_version_id"),
        foreign_keys=(("memory_id", "workspace_learning_memories", "CASCADE"), ("source_version_id", "workspace_competitor_content_versions", "SET NULL")),
    ),
    _spec(
        "content_learning_memory_feedback", "workspace_learning_memory_feedback",
        ("id", "project_id", "memory_id", "decision", "note", "created_at"),
        timestamps=("created_at",), integers=("id", "project_id", "memory_id"),
        foreign_keys=(("memory_id", "workspace_learning_memories", "CASCADE"),),
    ),
)

_SPEC_BY_SOURCE = {spec.source_table: spec for spec in EVIDENCE_TABLE_SPECS}


class EvidenceRepository(Protocol):
    def list_migration_snapshots(self) -> list[dict[str, Any]]: ...
    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None: ...


class SQLiteEvidenceRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = database_path

    def list_migration_snapshots(self) -> list[dict[str, Any]]:
        connection = initialize_database(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            snapshots: list[dict[str, Any]] = []
            for spec in EVIDENCE_TABLE_SPECS:
                snapshots.extend(
                    {"source_table": spec.source_table, **dict(row)}
                    for row in connection.execute(spec.source_select_sql).fetchall()
                )
            return snapshots
        finally:
            connection.close()

    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        raise RuntimeError("SQLite is the source of this staged evidence migration")


class PostgresEvidenceRepository:
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
        if column == "id" and spec.key_columns == ("id",):
            return "BIGSERIAL"
        if column in spec.integer_columns:
            return "BIGINT"
        if column in spec.float_columns:
            return "DOUBLE PRECISION"
        if column in spec.timestamp_columns:
            return "TIMESTAMPTZ"
        if column in spec.date_columns:
            return "DATE"
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
            if spec.key_columns == ("id",) and column == "id":
                definition += " PRIMARY KEY"
            definitions.append(definition)
        if spec.key_columns != ("id",):
            definitions.append(f"PRIMARY KEY({','.join(spec.key_columns)})")
        for column, parent, on_delete in spec.foreign_keys:
            definitions.append(f"FOREIGN KEY({column}) REFERENCES {parent}(id) ON DELETE {on_delete}")
        definitions.append("migrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        return f"CREATE TABLE IF NOT EXISTS {spec.target_table} ({','.join(definitions)})"

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            for spec in EVIDENCE_TABLE_SPECS:
                cursor.execute(self._ddl(spec))
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{spec.target_table}_project ON {spec.target_table}(project_id)"
                )
        self._initialized = True

    def list_migration_snapshots(self) -> list[dict[str, Any]]:
        self.initialize()
        snapshots: list[dict[str, Any]] = []
        with self._connect() as connection, connection.cursor() as cursor:
            for spec in EVIDENCE_TABLE_SPECS:
                cursor.execute(
                    f"SELECT {','.join(spec.columns)} FROM {spec.target_table} ORDER BY {','.join(spec.key_columns)}"
                )
                snapshots.extend(
                    {"source_table": spec.source_table, **dict(row)} for row in cursor.fetchall()
                )
        return snapshots

    @staticmethod
    def _snapshot_spec(snapshot: Mapping[str, Any]) -> EvidenceTableSpec:
        source_table = snapshot.get("source_table")
        if not isinstance(source_table, str) or source_table not in _SPEC_BY_SOURCE:
            raise ValueError("unknown evidence snapshot source_table")
        return _SPEC_BY_SOURCE[source_table]

    @staticmethod
    def _execute_upsert(cursor: Any, spec: EvidenceTableSpec, snapshot: Mapping[str, Any]) -> None:
        values = _evidence_values(spec, snapshot)
        updates = [column for column in spec.columns if column not in spec.key_columns]
        conflict = ",".join(spec.key_columns)
        placeholders = ["%s::jsonb" if column in spec.json_columns else "%s" for column in spec.columns]
        cursor.execute(
            f"INSERT INTO {spec.target_table}({','.join(spec.columns)},migrated_at) "
            f"VALUES({','.join(placeholders)},NOW()) ON CONFLICT({conflict}) DO UPDATE SET "
            + ",".join(f"{column}=excluded.{column}" for column in updates)
            + ",migrated_at=NOW()",
            tuple(values[column] for column in spec.columns),
        )

    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        self.initialize()
        spec = self._snapshot_spec(snapshot)
        with self._connect() as connection, connection.cursor() as cursor:
            self._execute_upsert(cursor, spec, snapshot)

    def upsert_migration_snapshots(self, snapshots: Sequence[Mapping[str, Any]]) -> None:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            for snapshot in snapshots:
                self._execute_upsert(cursor, self._snapshot_spec(snapshot), snapshot)

    def reset_identity(self) -> None:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            for spec in EVIDENCE_TABLE_SPECS:
                if spec.key_columns != ("id",):
                    continue
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


def _evidence_values(spec: EvidenceTableSpec, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    values = {column: snapshot.get(column) for column in spec.columns}
    project_id = values.get("project_id")
    if project_id is None or int(project_id) < 1:
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


def _comparable_evidence(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    spec = PostgresEvidenceRepository._snapshot_spec(snapshot)
    values = _evidence_values(spec, snapshot)
    for column in spec.timestamp_columns:
        values[column] = _timestamp(values[column])
    for column in spec.date_columns:
        values[column] = str(values[column] or "")
    for column in spec.json_columns:
        values[column] = _canonical_json(values[column]) if values[column] is not None else ""
    return {"source_table": spec.source_table, **values}


def _snapshot_key(snapshot: Mapping[str, Any]) -> str:
    spec = PostgresEvidenceRepository._snapshot_spec(snapshot)
    key = ":".join(str(snapshot[column]) for column in spec.key_columns)
    return f"{spec.source_table}:{key}"


class EvidenceMigrationService(SnapshotMigrationService):
    def __init__(self, source: EvidenceRepository, target: EvidenceRepository) -> None:
        super().__init__(source, target, key=_snapshot_key, normalize=_comparable_evidence)
