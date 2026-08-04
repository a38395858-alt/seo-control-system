"""Content-workflow, publishing, and GSC migration repositories for P6.5."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Protocol, Sequence

from seo_control.infrastructure.database import initialize_database

from .evidence import EvidenceTableSpec
from .migration import SnapshotMigrationService
from .projects import _timestamp


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
    return EvidenceTableSpec(
        source_table=source_table,
        target_table=target_table,
        columns=tuple(columns),
        key_columns=key_columns,
        source_select_sql=sql or f"SELECT {','.join(columns)} FROM {source_table} ORDER BY {','.join(key_columns)}",
        timestamp_columns=tuple(timestamps),
        integer_columns=tuple(integers),
        float_columns=tuple(floats),
        json_columns=tuple(jsons),
        date_columns=tuple(dates),
        foreign_keys=tuple(foreign_keys),
    )


WORKFLOW_TABLE_SPECS: tuple[EvidenceTableSpec, ...] = (
    _spec(
        "serp_title_samples", "workspace_serp_title_samples",
        ("id", "project_id", "keyword_id", "rank", "title", "normalized_title", "source", "source_type", "locale", "captured_at"),
        timestamps=("captured_at",), integers=("id", "project_id", "keyword_id", "rank"),
        foreign_keys=(("keyword_id", "workspace_keywords", "CASCADE"),),
    ),
    _spec(
        "title_generation_jobs", "workspace_title_generation_jobs",
        ("id", "project_id", "keyword_id", "status", "request_json", "provider", "model", "prompt_version", "requested_count", "generated_count", "error_code", "error_summary", "idempotency_key", "created_at", "started_at", "completed_at"),
        timestamps=("created_at", "started_at", "completed_at"), jsons=("request_json",),
        integers=("id", "project_id", "keyword_id", "requested_count", "generated_count"),
        foreign_keys=(("keyword_id", "workspace_keywords", "CASCADE"),),
    ),
    _spec(
        "keyword_title_candidates", "workspace_title_candidates",
        ("id", "project_id", "keyword_id", "generation_job_id", "title", "normalized_title", "title_type", "search_intent", "reason", "source_type", "quality_score", "quality_details_json", "rule_version", "status", "selected_at", "created_at", "updated_at", "deleted_at"),
        timestamps=("selected_at", "created_at", "updated_at", "deleted_at"), jsons=("quality_details_json",),
        integers=("id", "project_id", "keyword_id", "generation_job_id", "quality_score"),
        foreign_keys=(("keyword_id", "workspace_keywords", "CASCADE"), ("generation_job_id", "workspace_title_generation_jobs", "SET NULL")),
    ),
    _spec(
        "keyword_title_selection_events", "workspace_title_selection_events",
        ("id", "project_id", "keyword_id", "previous_candidate_id", "selected_candidate_id", "action", "reason", "created_at"),
        timestamps=("created_at",), integers=("id", "project_id", "keyword_id", "previous_candidate_id", "selected_candidate_id"),
        foreign_keys=(("keyword_id", "workspace_keywords", "CASCADE"), ("previous_candidate_id", "workspace_title_candidates", "SET NULL"), ("selected_candidate_id", "workspace_title_candidates", "SET NULL")),
    ),
    _spec(
        "content_assets", "workspace_content_assets",
        ("id", "project_id", "keyword_id", "selected_title_candidate_id", "title_snapshot", "locale", "country_code", "content_type", "status", "current_brief_id", "current_outline_id", "created_at", "updated_at", "deleted_at", "current_draft_id", "current_generation_run_id", "tags_json"),
        timestamps=("created_at", "updated_at", "deleted_at"), jsons=("tags_json",),
        integers=("id", "project_id", "keyword_id", "selected_title_candidate_id", "current_brief_id", "current_outline_id", "current_draft_id", "current_generation_run_id"),
        foreign_keys=(("keyword_id", "workspace_keywords", "CASCADE"), ("selected_title_candidate_id", "workspace_title_candidates", "RESTRICT")),
    ),
    _spec(
        "authority_source_library", "workspace_authority_sources",
        ("id", "project_id", "title", "source_type", "url", "publisher", "published_at", "content", "authority_level", "tags_json", "classification_json", "summary", "created_at", "updated_at"),
        timestamps=("published_at", "created_at", "updated_at"), jsons=("tags_json", "classification_json"), integers=("id", "project_id"),
    ),
    _spec(
        "agent_jobs", "workspace_agent_jobs",
        ("id", "project_id", "content_asset_id", "requested_action", "status", "current_node", "workflow_version", "input_json", "result_json", "error_summary", "created_at", "started_at", "completed_at", "updated_at", "checkpoint_json"),
        timestamps=("created_at", "started_at", "completed_at", "updated_at"), jsons=("input_json", "result_json", "checkpoint_json"),
        integers=("id", "project_id", "content_asset_id"), foreign_keys=(("content_asset_id", "workspace_content_assets", "SET NULL"),),
    ),
    _spec(
        "content_briefs", "workspace_content_briefs",
        ("project_id", "id", "content_asset_id", "target_audience", "business_goal", "target_length", "sources_json", "brief_json", "status", "created_at", "agent_job_id"),
        sql="SELECT assets.project_id AS project_id,briefs.id,briefs.content_asset_id,briefs.target_audience,briefs.business_goal,briefs.target_length,briefs.sources_json,briefs.brief_json,briefs.status,briefs.created_at,briefs.agent_job_id FROM content_briefs AS briefs JOIN content_assets AS assets ON assets.id=briefs.content_asset_id ORDER BY briefs.id",
        timestamps=("created_at",), jsons=("sources_json", "brief_json"), integers=("project_id", "id", "content_asset_id", "target_length", "agent_job_id"),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"), ("agent_job_id", "workspace_agent_jobs", "SET NULL")),
    ),
    _spec(
        "content_outlines", "workspace_content_outlines",
        ("project_id", "id", "content_asset_id", "brief_id", "status", "created_at", "agent_job_id"),
        sql="SELECT assets.project_id AS project_id,outlines.id,outlines.content_asset_id,outlines.brief_id,outlines.status,outlines.created_at,outlines.agent_job_id FROM content_outlines AS outlines JOIN content_assets AS assets ON assets.id=outlines.content_asset_id ORDER BY outlines.id",
        timestamps=("created_at",), integers=("project_id", "id", "content_asset_id", "brief_id", "agent_job_id"),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"), ("brief_id", "workspace_content_briefs", "RESTRICT"), ("agent_job_id", "workspace_agent_jobs", "SET NULL")),
    ),
    _spec(
        "content_outline_sections", "workspace_content_outline_sections",
        ("project_id", "id", "outline_id", "position", "heading", "purpose", "word_budget", "created_at", "section_json"),
        sql="SELECT assets.project_id AS project_id,sections.id,sections.outline_id,sections.position,sections.heading,sections.purpose,sections.word_budget,sections.created_at,sections.section_json FROM content_outline_sections AS sections JOIN content_outlines AS outlines ON outlines.id=sections.outline_id JOIN content_assets AS assets ON assets.id=outlines.content_asset_id ORDER BY sections.id",
        timestamps=("created_at",), jsons=("section_json",), integers=("project_id", "id", "outline_id", "position", "word_budget"),
        foreign_keys=(("outline_id", "workspace_content_outlines", "CASCADE"),),
    ),
    _spec(
        "content_generation_jobs", "workspace_content_generation_jobs",
        ("id", "project_id", "content_asset_id", "requested_action", "provider", "model", "status", "failed_stage", "error_summary", "started_at", "completed_at", "reviewer_provider", "reviewer_model", "routing_mode", "routing_summary"),
        timestamps=("started_at", "completed_at"), integers=("id", "project_id", "content_asset_id"),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"),),
    ),
    _spec(
        "content_generation_runs", "workspace_content_generation_runs",
        ("id", "project_id", "content_asset_id", "stage", "provider", "model", "status", "input_json", "output_json", "error_summary", "prompt_version", "started_at", "completed_at", "generation_job_id"),
        timestamps=("started_at", "completed_at"), jsons=("input_json", "output_json"), integers=("id", "project_id", "content_asset_id", "generation_job_id"),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"), ("generation_job_id", "workspace_content_generation_jobs", "SET NULL")),
    ),
    _spec(
        "content_drafts", "workspace_content_drafts",
        ("id", "project_id", "content_asset_id", "outline_id", "generation_run_id", "version", "title", "meta_description", "markdown", "sources_used_json", "unresolved_verify_json", "qa_json", "qa_status", "status", "created_at", "provider", "model", "generation_job_id", "parent_draft_id"),
        timestamps=("created_at",), jsons=("sources_used_json", "unresolved_verify_json", "qa_json"),
        integers=("id", "project_id", "content_asset_id", "outline_id", "generation_run_id", "version", "generation_job_id", "parent_draft_id"),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"), ("outline_id", "workspace_content_outlines", "SET NULL"), ("generation_run_id", "workspace_content_generation_runs", "SET NULL"), ("generation_job_id", "workspace_content_generation_jobs", "SET NULL")),
    ),
    _spec(
        "agent_steps", "workspace_agent_steps",
        ("project_id", "id", "job_id", "node_name", "attempt", "status", "model_provider", "model", "input_summary", "output_json", "error_summary", "started_at", "completed_at", "created_at", "input_json", "prompt_version", "duration_ms", "token_usage_json", "cost_micros"),
        sql="SELECT jobs.project_id AS project_id,steps.id,steps.job_id,steps.node_name,steps.attempt,steps.status,steps.model_provider,steps.model,steps.input_summary,steps.output_json,steps.error_summary,steps.started_at,steps.completed_at,steps.created_at,steps.input_json,steps.prompt_version,steps.duration_ms,steps.token_usage_json,steps.cost_micros FROM agent_steps AS steps JOIN agent_jobs AS jobs ON jobs.id=steps.job_id ORDER BY steps.id",
        timestamps=("started_at", "completed_at", "created_at"), jsons=("output_json", "input_json", "token_usage_json"),
        integers=("project_id", "id", "job_id", "attempt", "duration_ms", "cost_micros"), foreign_keys=(("job_id", "workspace_agent_jobs", "CASCADE"),),
    ),
    _spec(
        "agent_approval_requests", "workspace_agent_approvals",
        ("id", "project_id", "job_id", "approval_type", "payload_json", "status", "decided_by", "created_at", "decided_at", "consumed_at"),
        timestamps=("created_at", "decided_at", "consumed_at"), jsons=("payload_json",), integers=("id", "project_id", "job_id"),
        foreign_keys=(("job_id", "workspace_agent_jobs", "CASCADE"),),
    ),
    _spec(
        "agent_tool_audits", "workspace_agent_tool_audits",
        ("id", "project_id", "job_id", "tool_name", "status", "input_json", "output_json", "duration_ms", "error_summary", "created_at", "completed_at"),
        timestamps=("created_at", "completed_at"), jsons=("input_json", "output_json"), integers=("id", "project_id", "job_id", "duration_ms"),
        foreign_keys=(("job_id", "workspace_agent_jobs", "CASCADE"),),
    ),
    _spec(
        "content_memory_links", "workspace_content_memory_links",
        ("project_id", "id", "content_asset_id", "memory_id", "role", "relevance_score", "selected_by_model", "selected_by_user", "created_at"),
        sql="SELECT assets.project_id AS project_id,links.id,links.content_asset_id,links.memory_id,links.role,links.relevance_score,links.selected_by_model,links.selected_by_user,links.created_at FROM content_memory_links AS links JOIN content_assets AS assets ON assets.id=links.content_asset_id ORDER BY links.id",
        timestamps=("created_at",), integers=("project_id", "id", "content_asset_id", "memory_id", "selected_by_model", "selected_by_user"), floats=("relevance_score",),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"), ("memory_id", "workspace_learning_memories", "CASCADE")),
    ),
    _spec(
        "content_authority_source_links", "workspace_content_authority_links",
        ("id", "project_id", "content_asset_id", "authority_source_id", "section_heading", "claim_topic", "created_at"),
        timestamps=("created_at",), integers=("id", "project_id", "content_asset_id", "authority_source_id"),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"), ("authority_source_id", "workspace_authority_sources", "CASCADE")),
    ),
    _spec(
        "content_section_images", "workspace_content_section_images",
        ("id", "project_id", "content_asset_id", "draft_id", "section_heading", "position", "prompt", "alt_text", "status", "image_url", "provider", "model", "error_summary", "created_at", "updated_at", "seo_filename"),
        timestamps=("created_at", "updated_at"), integers=("id", "project_id", "content_asset_id", "draft_id", "position"),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"), ("draft_id", "workspace_content_drafts", "CASCADE")),
    ),
    _spec(
        "content_generation_basis_reports", "workspace_content_generation_basis_reports",
        ("id", "project_id", "agent_job_id", "content_asset_id", "draft_id", "report_json", "created_at", "updated_at"),
        timestamps=("created_at", "updated_at"), jsons=("report_json",), integers=("id", "project_id", "agent_job_id", "content_asset_id", "draft_id"),
        foreign_keys=(("agent_job_id", "workspace_agent_jobs", "CASCADE"), ("content_asset_id", "workspace_content_assets", "CASCADE"), ("draft_id", "workspace_content_drafts", "SET NULL")),
    ),
    _spec(
        "project_wordpress_configs", "workspace_wordpress_configs",
        ("project_id", "site_url", "username", "requires_reauthorization", "updated_at", "last_tested_at"), keys=("project_id",),
        sql="SELECT project_id,site_url,username,CASE WHEN trim(COALESCE(application_password,''))<>'' THEN 1 ELSE 0 END AS requires_reauthorization,updated_at,last_tested_at FROM project_wordpress_configs ORDER BY project_id",
        timestamps=("updated_at", "last_tested_at"), integers=("project_id", "requires_reauthorization"),
    ),
    _spec(
        "content_publish_gate_reports", "workspace_content_publish_gate_reports",
        ("id", "project_id", "content_asset_id", "draft_id", "requested_status", "report_json", "status", "created_at"),
        timestamps=("created_at",), jsons=("report_json",), integers=("id", "project_id", "content_asset_id", "draft_id"),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"), ("draft_id", "workspace_content_drafts", "CASCADE")),
    ),
    _spec(
        "content_wordpress_publications", "workspace_wordpress_publications",
        ("id", "project_id", "content_asset_id", "draft_id", "wordpress_post_id", "wordpress_url", "status", "error_summary", "created_at"),
        timestamps=("created_at",), integers=("id", "project_id", "content_asset_id", "draft_id", "wordpress_post_id"),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"), ("draft_id", "workspace_content_drafts", "CASCADE")),
    ),
    _spec(
        "project_gsc_properties", "workspace_gsc_properties",
        ("project_id", "property_url", "updated_at"), keys=("project_id",), timestamps=("updated_at",), integers=("project_id",),
    ),
    _spec(
        "project_gsc_query_rows", "workspace_gsc_query_rows",
        ("id", "project_id", "property_url", "query", "page_url", "clicks", "impressions", "ctr", "position", "collected_at"),
        timestamps=("collected_at",), integers=("id", "project_id"), floats=("clicks", "impressions", "ctr", "position"),
    ),
    _spec(
        "content_gsc_performance_snapshots", "workspace_content_gsc_snapshots",
        ("id", "project_id", "content_asset_id", "draft_id", "published_url", "window_days", "clicks", "impressions", "ctr", "average_position", "query_count", "learning_status", "summary", "collected_at"),
        timestamps=("collected_at",), integers=("id", "project_id", "content_asset_id", "draft_id", "window_days", "query_count"), floats=("clicks", "impressions", "ctr", "average_position"),
        foreign_keys=(("content_asset_id", "workspace_content_assets", "CASCADE"), ("draft_id", "workspace_content_drafts", "SET NULL")),
    ),
    _spec(
        "content_gsc_performance_rows", "workspace_content_gsc_rows",
        ("project_id", "id", "snapshot_id", "query", "page_url", "clicks", "impressions", "ctr", "position"),
        sql="SELECT snapshots.project_id AS project_id,rows.id,rows.snapshot_id,rows.query,rows.page_url,rows.clicks,rows.impressions,rows.ctr,rows.position FROM content_gsc_performance_rows AS rows JOIN content_gsc_performance_snapshots AS snapshots ON snapshots.id=rows.snapshot_id ORDER BY rows.id",
        integers=("project_id", "id", "snapshot_id"), floats=("clicks", "impressions", "ctr", "position"),
        foreign_keys=(("snapshot_id", "workspace_content_gsc_snapshots", "CASCADE"),),
    ),
)

_SPEC_BY_SOURCE = {spec.source_table: spec for spec in WORKFLOW_TABLE_SPECS}


class WorkflowRepository(Protocol):
    def list_migration_snapshots(self) -> list[dict[str, Any]]: ...
    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None: ...


class SQLiteWorkflowRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = database_path

    def list_migration_snapshots(self) -> list[dict[str, Any]]:
        connection = initialize_database(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            snapshots: list[dict[str, Any]] = []
            for spec in WORKFLOW_TABLE_SPECS:
                snapshots.extend(
                    {"source_table": spec.source_table, **dict(row)}
                    for row in connection.execute(spec.source_select_sql).fetchall()
                )
            return snapshots
        finally:
            connection.close()

    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        raise RuntimeError("SQLite is the source of this staged workflow migration")


class PostgresWorkflowRepository:
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
            for spec in WORKFLOW_TABLE_SPECS:
                cursor.execute(self._ddl(spec))
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{spec.target_table}_project ON {spec.target_table}(project_id)"
                )
        self._initialized = True

    def list_migration_snapshots(self) -> list[dict[str, Any]]:
        self.initialize()
        snapshots: list[dict[str, Any]] = []
        with self._connect() as connection, connection.cursor() as cursor:
            for spec in WORKFLOW_TABLE_SPECS:
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
            raise ValueError("unknown workflow snapshot source_table")
        return _SPEC_BY_SOURCE[source_table]

    @staticmethod
    def _execute_upsert(cursor: Any, spec: EvidenceTableSpec, snapshot: Mapping[str, Any]) -> None:
        values = _workflow_values(spec, snapshot)
        updates = [column for column in spec.columns if column not in spec.key_columns]
        placeholders = ["%s::jsonb" if column in spec.json_columns else "%s" for column in spec.columns]
        cursor.execute(
            f"INSERT INTO {spec.target_table}({','.join(spec.columns)},migrated_at) "
            f"VALUES({','.join(placeholders)},NOW()) ON CONFLICT({','.join(spec.key_columns)}) DO UPDATE SET "
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
            for spec in WORKFLOW_TABLE_SPECS:
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


def _workflow_values(spec: EvidenceTableSpec, snapshot: Mapping[str, Any]) -> dict[str, Any]:
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


def _comparable_workflow(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    spec = PostgresWorkflowRepository._snapshot_spec(snapshot)
    values = _workflow_values(spec, snapshot)
    for column in spec.timestamp_columns:
        values[column] = _timestamp(values[column])
    for column in spec.date_columns:
        values[column] = str(values[column] or "")
    return {"source_table": spec.source_table, **values}


def _snapshot_key(snapshot: Mapping[str, Any]) -> str:
    spec = PostgresWorkflowRepository._snapshot_spec(snapshot)
    return f"{spec.source_table}:" + ":".join(str(snapshot[column]) for column in spec.key_columns)


class WorkflowMigrationService(SnapshotMigrationService):
    def __init__(self, source: WorkflowRepository, target: WorkflowRepository) -> None:
        super().__init__(source, target, key=_snapshot_key, normalize=_comparable_workflow)
