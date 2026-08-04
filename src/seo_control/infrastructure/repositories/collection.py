"""Collection-plan and competitor-URL repositories for staged migration."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Protocol

from seo_control.infrastructure.database import initialize_database

from .migration import SnapshotMigrationService
from .projects import _timestamp


PLAN_COLUMNS = (
    "id", "project_id", "source_type", "source_value", "status", "schedule", "settings_json",
    "discovered_count", "last_discovered_at", "created_at", "updated_at",
)
CATALOG_COLUMNS = (
    "id", "project_id", "normalized_url", "url", "domain", "search_title",
    "collection_status", "exclusion_reason", "last_rank", "last_query", "memory_id",
    "discovered_count", "first_seen_at", "last_seen_at", "last_collected_at",
)
CATALOG_API_COLUMNS = (
    "id", "url", "domain", "search_title", "collection_status", "exclusion_reason",
    "last_rank", "last_query", "memory_id", "discovered_count", "first_seen_at",
    "last_seen_at", "last_collected_at",
)
PLAN_TIMESTAMP_COLUMNS = ("last_discovered_at", "created_at", "updated_at")
CATALOG_TIMESTAMP_COLUMNS = ("first_seen_at", "last_seen_at", "last_collected_at")


class CollectionRepository(Protocol):
    def list_collection_plans(self, project_id: int) -> list[dict[str, Any]]: ...
    def list_catalog(self, project_id: int) -> list[dict[str, Any]]: ...
    def list_migration_snapshots(self) -> list[dict[str, Any]]: ...
    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None: ...


def _plan_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(row)
    try:
        settings = json.loads(value.pop("settings_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        settings = {}
    value["settings"] = settings if isinstance(settings, dict) else {}
    return value


class SQLiteCollectionRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = database_path

    def _connection(self) -> sqlite3.Connection:
        connection = initialize_database(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _assert_project(connection: sqlite3.Connection, project_id: int) -> None:
        if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise ValueError("project does not exist")

    def list_collection_plans(self, project_id: int) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            self._assert_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM collection_plans WHERE project_id=? ORDER BY updated_at DESC,id DESC",
                (project_id,),
            ).fetchall()
            return [_plan_payload(row) for row in rows]
        finally:
            connection.close()

    def list_catalog(self, project_id: int) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            self._assert_project(connection, project_id)
            rows = connection.execute(
                f"SELECT {','.join(CATALOG_API_COLUMNS)} FROM competitor_url_catalog "
                "WHERE project_id=? ORDER BY last_seen_at DESC,id DESC",
                (project_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def list_migration_snapshots(self) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            plans = connection.execute(
                f"SELECT {','.join(PLAN_COLUMNS)} FROM collection_plans ORDER BY id"
            ).fetchall()
            catalog = connection.execute(
                f"SELECT {','.join(CATALOG_COLUMNS)} FROM competitor_url_catalog ORDER BY id"
            ).fetchall()
            return [
                *[{"entity": "plan", **dict(row)} for row in plans],
                *[{"entity": "catalog", **dict(row)} for row in catalog],
            ]
        finally:
            connection.close()

    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        raise RuntimeError("SQLite is the source of this staged collection migration")


class PostgresCollectionRepository:
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
                """CREATE TABLE IF NOT EXISTS workspace_collection_plans (
                    id BIGSERIAL PRIMARY KEY,
                    project_id BIGINT NOT NULL REFERENCES workspace_projects(id) ON DELETE CASCADE,
                    source_type TEXT NOT NULL,source_value TEXT NOT NULL,status TEXT NOT NULL,
                    schedule TEXT NOT NULL,settings_json TEXT NOT NULL DEFAULT '{}',
                    discovered_count BIGINT NOT NULL DEFAULT 0,last_discovered_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL,
                    migrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(project_id,source_type,source_value)
                )"""
            )
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS workspace_competitor_urls (
                    id BIGSERIAL PRIMARY KEY,
                    project_id BIGINT NOT NULL REFERENCES workspace_projects(id) ON DELETE CASCADE,
                    normalized_url TEXT NOT NULL,url TEXT NOT NULL,domain TEXT NOT NULL DEFAULT '',
                    search_title TEXT NOT NULL DEFAULT '',collection_status TEXT NOT NULL,
                    exclusion_reason TEXT NOT NULL DEFAULT '',last_rank BIGINT,last_query TEXT NOT NULL DEFAULT '',
                    memory_id BIGINT,discovered_count BIGINT NOT NULL DEFAULT 1,
                    first_seen_at TIMESTAMPTZ NOT NULL,last_seen_at TIMESTAMPTZ NOT NULL,
                    last_collected_at TIMESTAMPTZ,migrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(project_id,normalized_url)
                )"""
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_workspace_collection_plans_project ON workspace_collection_plans(project_id,status,id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_workspace_competitor_urls_project ON workspace_competitor_urls(project_id,collection_status,id)"
            )
        self._initialized = True

    @staticmethod
    def _rows(cursor: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in cursor.fetchall()]

    def _assert_project(self, cursor: Any, project_id: int) -> None:
        cursor.execute("SELECT 1 FROM workspace_projects WHERE id=%s", (project_id,))
        if cursor.fetchone() is None:
            raise ValueError("project does not exist")

    def list_collection_plans(self, project_id: int) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            self._assert_project(cursor, project_id)
            cursor.execute(
                f"SELECT {','.join(PLAN_COLUMNS)} FROM workspace_collection_plans "
                "WHERE project_id=%s ORDER BY updated_at DESC,id DESC",
                (project_id,),
            )
            return [_plan_payload(row) for row in self._rows(cursor)]

    def list_catalog(self, project_id: int) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            self._assert_project(cursor, project_id)
            cursor.execute(
                f"SELECT {','.join(CATALOG_API_COLUMNS)} FROM workspace_competitor_urls "
                "WHERE project_id=%s ORDER BY last_seen_at DESC,id DESC",
                (project_id,),
            )
            return self._rows(cursor)

    def list_migration_snapshots(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT {','.join(PLAN_COLUMNS)} FROM workspace_collection_plans ORDER BY id")
            plans = self._rows(cursor)
            cursor.execute(f"SELECT {','.join(CATALOG_COLUMNS)} FROM workspace_competitor_urls ORDER BY id")
            catalog = self._rows(cursor)
            return [
                *[{"entity": "plan", **row} for row in plans],
                *[{"entity": "catalog", **row} for row in catalog],
            ]

    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        entity = snapshot.get("entity")
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            if entity == "plan":
                values = _plan_values(snapshot)
                cursor.execute(
                    """INSERT INTO workspace_collection_plans(
                        id,project_id,source_type,source_value,status,schedule,settings_json,
                        discovered_count,last_discovered_at,created_at,updated_at,migrated_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id,
                        source_type=excluded.source_type,source_value=excluded.source_value,status=excluded.status,
                        schedule=excluded.schedule,settings_json=excluded.settings_json,
                        discovered_count=excluded.discovered_count,last_discovered_at=excluded.last_discovered_at,
                        created_at=excluded.created_at,updated_at=excluded.updated_at,migrated_at=NOW()""",
                    tuple(values[column] for column in PLAN_COLUMNS),
                )
            elif entity == "catalog":
                values = _catalog_values(snapshot)
                cursor.execute(
                    """INSERT INTO workspace_competitor_urls(
                        id,project_id,normalized_url,url,domain,search_title,collection_status,
                        exclusion_reason,last_rank,last_query,memory_id,discovered_count,
                        first_seen_at,last_seen_at,last_collected_at,migrated_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id,
                        normalized_url=excluded.normalized_url,url=excluded.url,domain=excluded.domain,
                        search_title=excluded.search_title,collection_status=excluded.collection_status,
                        exclusion_reason=excluded.exclusion_reason,last_rank=excluded.last_rank,
                        last_query=excluded.last_query,memory_id=excluded.memory_id,
                        discovered_count=excluded.discovered_count,first_seen_at=excluded.first_seen_at,
                        last_seen_at=excluded.last_seen_at,last_collected_at=excluded.last_collected_at,
                        migrated_at=NOW()""",
                    tuple(values[column] for column in CATALOG_COLUMNS),
                )
            else:
                raise ValueError("collection snapshot entity must be plan or catalog")

    def reset_identity(self) -> None:
        self.initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            for table in ("workspace_collection_plans", "workspace_competitor_urls"):
                cursor.execute(
                    f"""SELECT setval(pg_get_serial_sequence('{table}','id'),
                       GREATEST(COALESCE(MAX(id),1),1),COALESCE(MAX(id),0)>0) FROM {table}"""
                )


def _plan_values(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "status": "active", "schedule": "manual", "settings_json": "{}",
        "discovered_count": 0, "last_discovered_at": None,
    }
    values = {column: snapshot.get(column, defaults.get(column)) for column in PLAN_COLUMNS}
    _validate_identity(values, "collection plan")
    if values["source_type"] not in {"domain", "keyword", "first_party"}:
        raise ValueError("collection plan source_type is invalid")
    if not isinstance(values["source_value"], str) or not values["source_value"].strip():
        raise ValueError("collection plan source_value is required")
    return values


def _catalog_values(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "domain": "", "search_title": "", "collection_status": "discovered",
        "exclusion_reason": "", "last_rank": None, "last_query": "", "memory_id": None,
        "discovered_count": 1, "last_collected_at": None,
    }
    values = {column: snapshot.get(column, defaults.get(column)) for column in CATALOG_COLUMNS}
    _validate_identity(values, "competitor URL")
    if not isinstance(values["normalized_url"], str) or not values["normalized_url"].strip():
        raise ValueError("competitor URL normalized_url is required")
    if not isinstance(values["url"], str) or not values["url"].strip():
        raise ValueError("competitor URL url is required")
    return values


def _validate_identity(values: Mapping[str, Any], label: str) -> None:
    if not isinstance(values["id"], int) or int(values["id"]) < 1:
        raise ValueError(f"{label} snapshot id must be positive")
    if not isinstance(values["project_id"], int) or int(values["project_id"]) < 1:
        raise ValueError(f"{label} snapshot project_id must be positive")


def _canonical_json(value: Any) -> str:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return str(value or "")
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _comparable_collection(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    entity = snapshot.get("entity")
    if entity == "plan":
        values = _plan_values(snapshot)
        for column in PLAN_TIMESTAMP_COLUMNS:
            values[column] = _timestamp(values[column])
        values["discovered_count"] = int(values["discovered_count"] or 0)
        values["settings_json"] = _canonical_json(values["settings_json"])
    elif entity == "catalog":
        values = _catalog_values(snapshot)
        for column in CATALOG_TIMESTAMP_COLUMNS:
            values[column] = _timestamp(values[column])
        for column in ("last_rank", "memory_id"):
            values[column] = None if values[column] is None else int(values[column])
        values["discovered_count"] = int(values["discovered_count"] or 0)
    else:
        raise ValueError("collection snapshot entity must be plan or catalog")
    values["id"] = int(values["id"])
    values["project_id"] = int(values["project_id"])
    return {"entity": entity, **values}


class CollectionMigrationService(SnapshotMigrationService):
    def __init__(self, source: CollectionRepository, target: CollectionRepository) -> None:
        super().__init__(
            source,
            target,
            key=lambda row: f"{row['entity']}:{int(row['id'])}",
            normalize=_comparable_collection,
        )
