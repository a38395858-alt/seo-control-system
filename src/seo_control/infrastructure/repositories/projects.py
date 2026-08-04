"""Project Repository contract and first SQLite/PostgreSQL implementations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Protocol

from seo_control.infrastructure.database import initialize_database


PROJECT_COLUMNS = (
    "id", "name", "site_url", "industry", "default_country", "default_language",
    "created_at", "updated_at",
)
SUMMARY_COLUMNS = PROJECT_COLUMNS + (
    "keyword_count", "selected_title_count", "content_count", "knowledge_count",
    "latest_content_status",
)
EDITABLE_FIELDS = frozenset({"name", "site_url", "industry", "default_country", "default_language"})


class ProjectRepository(Protocol):
    def create_project(self, *, name: str, site_url: str, industry: str, country_code: str, language_code: str) -> dict[str, Any]: ...
    def list_projects(self) -> list[dict[str, Any]]: ...
    def list_project_summaries(self) -> list[dict[str, Any]]: ...
    def update_project(self, project_id: int, fields: Mapping[str, str]) -> dict[str, Any]: ...
    def delete_project(self, project_id: int) -> None: ...
    def upsert_project_snapshot(self, snapshot: Mapping[str, Any]) -> None: ...


class SQLiteProjectRepository:
    """Current production implementation; every operation closes its handle."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = database_path

    def _connection(self) -> sqlite3.Connection:
        connection = initialize_database(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def create_project(self, *, name: str, site_url: str, industry: str, country_code: str, language_code: str) -> dict[str, Any]:
        connection = self._connection()
        try:
            with connection:
                cursor = connection.execute(
                    """INSERT INTO projects(name,site_url,industry,default_country,default_language,negative_terms_json)
                       VALUES(?,?,?,?,?,'[]')""",
                    (name, site_url, industry, country_code, language_code),
                )
                row = connection.execute(
                    "SELECT id,name,site_url,industry,default_country,default_language,created_at,updated_at FROM projects WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()
                return dict(row)
        finally:
            connection.close()

    def list_projects(self) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT id,name,site_url,industry,default_country,default_language,created_at,updated_at FROM projects ORDER BY id DESC"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def list_project_summaries(self) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            rows = connection.execute(
                """SELECT projects.id,projects.name,projects.site_url,projects.industry,projects.default_country,projects.default_language,projects.created_at,projects.updated_at,
                    (SELECT COUNT(*) FROM keywords WHERE project_id=projects.id AND deleted_at IS NULL) AS keyword_count,
                    (SELECT COUNT(*) FROM keyword_title_candidates WHERE project_id=projects.id AND status='selected' AND deleted_at IS NULL) AS selected_title_count,
                    (SELECT COUNT(*) FROM content_assets WHERE project_id=projects.id AND deleted_at IS NULL) AS content_count,
                    (SELECT COUNT(*) FROM project_knowledge_documents WHERE project_id=projects.id AND status='ready') AS knowledge_count,
                    (SELECT status FROM content_assets WHERE project_id=projects.id AND deleted_at IS NULL ORDER BY updated_at DESC,id DESC LIMIT 1) AS latest_content_status
                    FROM projects ORDER BY projects.updated_at DESC,projects.id DESC"""
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def update_project(self, project_id: int, fields: Mapping[str, str]) -> dict[str, Any]:
        updates = [(key, value) for key, value in fields.items() if key in EDITABLE_FIELDS]
        if not updates:
            raise ValueError("At least one project field is required.")
        connection = self._connection()
        try:
            with connection:
                assignments = ",".join(f"{key}=?" for key, _value in updates)
                statement = f"UPDATE projects SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE id=?"
                cursor = connection.execute(statement, (*[value for _key, value in updates], project_id))
                if cursor.rowcount != 1:
                    raise ValueError("project does not exist")
                row = connection.execute(
                    "SELECT id,name,site_url,industry,default_country,default_language,created_at,updated_at FROM projects WHERE id=?",
                    (project_id,),
                ).fetchone()
                return dict(row)
        finally:
            connection.close()

    def delete_project(self, project_id: int) -> None:
        connection = self._connection()
        try:
            with connection:
                cursor = connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
                if cursor.rowcount != 1:
                    raise ValueError("project does not exist")
        finally:
            connection.close()

    def upsert_project_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        values = _snapshot_values(snapshot)
        connection = self._connection()
        try:
            with connection:
                connection.execute(
                    """INSERT INTO projects(id,name,site_url,industry,default_country,default_language,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name,site_url=excluded.site_url,industry=excluded.industry,
                         default_country=excluded.default_country,default_language=excluded.default_language,updated_at=excluded.updated_at""",
                    tuple(values[column] for column in PROJECT_COLUMNS),
                )
        finally:
            connection.close()


class PostgresProjectRepository:
    """PostgreSQL target kept separate until all child modules are migrated."""

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
        except ImportError as error:  # pragma: no cover - production-only dependency
            raise RuntimeError("install requirements-postgres.txt before PostgreSQL migration") from error
        return connect(self.database_url, row_factory=dict_row)

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS workspace_projects (
                    id BIGSERIAL PRIMARY KEY,name TEXT NOT NULL,site_url TEXT,industry TEXT NOT NULL DEFAULT '',
                    default_country TEXT NOT NULL DEFAULT 'US',default_language TEXT NOT NULL DEFAULT 'en',
                    keyword_count BIGINT NOT NULL DEFAULT 0,selected_title_count BIGINT NOT NULL DEFAULT 0,
                    content_count BIGINT NOT NULL DEFAULT 0,knowledge_count BIGINT NOT NULL DEFAULT 0,
                    latest_content_status TEXT,source_system TEXT NOT NULL DEFAULT 'seo_control_sqlite',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    migrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )"""
            )
        self._initialized = True

    @staticmethod
    def _rows(cursor: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in cursor.fetchall()]

    def create_project(self, *, name: str, site_url: str, industry: str, country_code: str, language_code: str) -> dict[str, Any]:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO workspace_projects(name,site_url,industry,default_country,default_language)
                   VALUES(%s,%s,%s,%s,%s) RETURNING id,name,site_url,industry,default_country,default_language,created_at,updated_at""",
                (name, site_url, industry, country_code, language_code),
            )
            return dict(cursor.fetchone())

    def list_projects(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id,name,site_url,industry,default_country,default_language,created_at,updated_at FROM workspace_projects ORDER BY id DESC")
            return self._rows(cursor)

    def list_project_summaries(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id,name,site_url,industry,default_country,default_language,created_at,updated_at,keyword_count,selected_title_count,content_count,knowledge_count,latest_content_status FROM workspace_projects ORDER BY updated_at DESC,id DESC")
            return self._rows(cursor)

    def update_project(self, project_id: int, fields: Mapping[str, str]) -> dict[str, Any]:
        updates = [(key, value) for key, value in fields.items() if key in EDITABLE_FIELDS]
        if not updates:
            raise ValueError("At least one project field is required.")
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            assignments = ",".join(f"{key}=%s" for key, _value in updates)
            statement = (
                f"UPDATE workspace_projects SET {assignments},updated_at=NOW() WHERE id=%s "
                "RETURNING id,name,site_url,industry,default_country,default_language,created_at,updated_at"
            )
            cursor.execute(
                statement,
                (*[value for _key, value in updates], project_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("project does not exist")
            return dict(row)

    def delete_project(self, project_id: int) -> None:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM workspace_projects WHERE id=%s", (project_id,))
            if cursor.rowcount != 1:
                raise ValueError("project does not exist")

    def upsert_project_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        values = _snapshot_values(snapshot)
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO workspace_projects(
                       id,name,site_url,industry,default_country,default_language,created_at,updated_at,
                       keyword_count,selected_title_count,content_count,knowledge_count,latest_content_status,migrated_at
                   ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,site_url=excluded.site_url,industry=excluded.industry,
                     default_country=excluded.default_country,default_language=excluded.default_language,
                     keyword_count=excluded.keyword_count,selected_title_count=excluded.selected_title_count,
                     content_count=excluded.content_count,knowledge_count=excluded.knowledge_count,
                     latest_content_status=excluded.latest_content_status,updated_at=excluded.updated_at,migrated_at=NOW()""",
                tuple(values[column] for column in SUMMARY_COLUMNS),
            )

    def reset_identity(self) -> None:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT setval(pg_get_serial_sequence('workspace_projects','id'),
                       GREATEST(COALESCE(MAX(id),1),1),COALESCE(MAX(id),0)>0) FROM workspace_projects"""
            )


@dataclass(frozen=True)
class ProjectMigrationReport:
    source_count: int
    target_count: int
    migrated_count: int
    missing_ids: tuple[int, ...]
    extra_ids: tuple[int, ...]
    mismatches: tuple[dict[str, Any], ...]
    applied: bool

    @property
    def valid(self) -> bool:
        return not self.missing_ids and not self.extra_ids and not self.mismatches and self.source_count == self.target_count

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "valid": self.valid}


class ProjectMigrationService:
    def __init__(self, source: ProjectRepository, target: ProjectRepository) -> None:
        self.source = source
        self.target = target

    def migrate(self, *, apply: bool = False) -> ProjectMigrationReport:
        source_rows = self.source.list_project_summaries()
        if not apply:
            return ProjectMigrationReport(len(source_rows), 0, 0, (), (), (), False)
        for row in source_rows:
            self.target.upsert_project_snapshot(row)
        reset = getattr(self.target, "reset_identity", None)
        if callable(reset):
            reset()
        return self.validate(migrated_count=len(source_rows), applied=True)

    def validate(self, *, migrated_count: int = 0, applied: bool = False) -> ProjectMigrationReport:
        source = {int(row["id"]): _comparable(row) for row in self.source.list_project_summaries()}
        target = {int(row["id"]): _comparable(row) for row in self.target.list_project_summaries()}
        missing = tuple(sorted(source.keys() - target.keys()))
        extra = tuple(sorted(target.keys() - source.keys()))
        mismatches: list[dict[str, Any]] = []
        for project_id in sorted(source.keys() & target.keys()):
            changed = {key: {"source": source[project_id][key], "target": target[project_id][key]} for key in SUMMARY_COLUMNS if source[project_id][key] != target[project_id][key]}
            if changed:
                mismatches.append({"project_id": project_id, "fields": changed})
        return ProjectMigrationReport(len(source), len(target), migrated_count, missing, extra, tuple(mismatches), applied)


def _snapshot_values(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "site_url": None, "industry": "", "default_country": "US", "default_language": "en",
        "keyword_count": 0, "selected_title_count": 0, "content_count": 0, "knowledge_count": 0,
        "latest_content_status": None,
    }
    values = {column: snapshot.get(column, defaults.get(column)) for column in SUMMARY_COLUMNS}
    if not isinstance(values["id"], int) or int(values["id"]) < 1:
        raise ValueError("project snapshot id must be positive")
    if not isinstance(values["name"], str) or not values["name"].strip():
        raise ValueError("project snapshot name is required")
    return values


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        normalized = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return normalized.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text.replace(" ", "T")
    normalized = parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return normalized.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _comparable(row: Mapping[str, Any]) -> dict[str, Any]:
    value = _snapshot_values(row)
    for field in ("created_at", "updated_at"):
        value[field] = _timestamp(value[field])
    for field in ("keyword_count", "selected_title_count", "content_count", "knowledge_count"):
        value[field] = int(value[field] or 0)
    return value
