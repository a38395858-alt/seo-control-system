"""Runtime database selection, cutover gates, and redacted shadow-read state.

The local workspace is still SQLite-first.  This module deliberately keeps the
runtime switch separate from migration code so a DATABASE_URL alone can never
silently change the production data source.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse

from seo_control.infrastructure.database import initialize_database


class RuntimeDatabaseMode(str, Enum):
    SQLITE = "sqlite"
    SHADOW = "shadow"
    POSTGRES = "postgres"


DEFAULT_REQUIRED_SHADOW_PASSES = 10
RUNTIME_COVERAGE = {
    "project_repository": True,
    "keyword_read_delete_repository": True,
    "collection_read_repository": True,
    "content_workflow_postgres_compatibility": True,
    "memory_learning_postgres_compatibility": True,
    "gsc_and_wordpress_postgres_compatibility": True,
    "scheduler_and_task_queue_postgres_compatibility": True,
    "local_credential_vault": True,
}


def describe_database_url(database_url: str | None) -> dict[str, Any] | None:
    """Return connection metadata without usernames, passwords, or query values."""
    if not database_url:
        return None
    parsed = urlparse(database_url)
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "database": unquote(parsed.path.lstrip("/")),
    }


def redact_runtime_error(error: Exception | str, database_url: str | None = None) -> str:
    text = str(error)
    if database_url:
        text = text.replace(database_url, "[DATABASE_URL]")
    text = re.sub(r"(?i)(password\s*=\s*)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(postgres(?:ql)?://)[^@\s]+@", r"\1[REDACTED]@", text)
    return text[:500]


class RuntimeDatabaseController:
    """Own the effective runtime mode and persist only non-sensitive audit data."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        database_url: str | None = None,
        state_path: str | Path | None = None,
        requested_mode: str | None = None,
        required_shadow_passes: int = DEFAULT_REQUIRED_SHADOW_PASSES,
        full_repository_coverage: bool | None = None,
        migration_suite_factory: Callable[..., Any] | None = None,
        postgres_health_check: Callable[[str], None] | None = None,
        credential_vault: Any | None = None,
    ) -> None:
        self.database_path = Path(database_path) if str(database_path) != ":memory:" else Path(":memory:")
        self.database_url = database_url or os.getenv("DATABASE_URL")
        default_state = (
            Path(os.getenv("SEO_RUNTIME_SWITCH_STATE_PATH", ""))
            if os.getenv("SEO_RUNTIME_SWITCH_STATE_PATH")
            else (self.database_path.parent / "runtime-database-state.json" if str(database_path) != ":memory:" else None)
        )
        self.state_path = Path(state_path) if state_path is not None else default_state
        self.required_shadow_passes = max(1, int(required_shadow_passes))
        self.full_repository_coverage = (
            all(RUNTIME_COVERAGE.values())
            if full_repository_coverage is None else bool(full_repository_coverage)
        )
        if migration_suite_factory is None:
            from seo_control.infrastructure.runtime_postgres import PostgresRuntimeMigration
            migration_suite_factory = PostgresRuntimeMigration
        self.migration_suite_factory = migration_suite_factory
        self.postgres_health_check = postgres_health_check or self._default_postgres_health_check
        self.credential_vault = credential_vault
        self._lock = threading.RLock()
        self._state = self._load_state()

        if self._state.get("mode") == RuntimeDatabaseMode.POSTGRES.value and not self._persisted_cutover_is_approved():
            self._state["mode"] = RuntimeDatabaseMode.SQLITE.value
            self._state["startup_warning"] = "unapproved persisted postgres mode was replaced with the SQLite safety default"
            self._save_state()

        configured = requested_mode or os.getenv("SEO_RUNTIME_DATABASE_MODE")
        if configured:
            mode = self._parse_mode(configured)
            # PostgreSQL activation is only legal through cutover(), never by
            # adding an environment variable to a service command.
            if mode is RuntimeDatabaseMode.POSTGRES and self._state.get("mode") != RuntimeDatabaseMode.POSTGRES.value:
                self._state["startup_warning"] = "postgres mode ignored until an approved cutover is persisted"
                mode = RuntimeDatabaseMode.SQLITE
            self._state["mode"] = mode.value
            self._save_state()

    @staticmethod
    def _parse_mode(value: str) -> RuntimeDatabaseMode:
        try:
            return RuntimeDatabaseMode(str(value).strip().casefold())
        except ValueError as error:
            raise ValueError("runtime database mode must be sqlite, shadow, or postgres") from error

    def _persisted_cutover_is_approved(self) -> bool:
        gate = self._state.get("last_gate")
        transition = self._state.get("last_transition")
        return (
            isinstance(gate, Mapping) and gate.get("ready") is True
            and isinstance(transition, Mapping)
            and transition.get("to") == RuntimeDatabaseMode.POSTGRES.value
        )

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "mode": RuntimeDatabaseMode.SQLITE.value,
            "updated_at": self._now(),
            "shadow": {
                "total_checks": 0,
                "passed_checks": 0,
                "failed_checks": 0,
                "consecutive_passes": 0,
                "recent": [],
            },
            "last_gate": None,
            "last_transition": None,
        }

    def _load_state(self) -> dict[str, Any]:
        if self.state_path is None or not self.state_path.exists():
            return self._default_state()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("state is not an object")
            self._parse_mode(str(payload.get("mode", "sqlite")))
            payload.setdefault("shadow", self._default_state()["shadow"])
            return payload
        except (OSError, ValueError, json.JSONDecodeError):
            state = self._default_state()
            state["startup_warning"] = "runtime state was unreadable; SQLite safety default applied"
            return state

    def _save_state(self) -> None:
        self._state["updated_at"] = self._now()
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @property
    def mode(self) -> RuntimeDatabaseMode:
        with self._lock:
            return self._parse_mode(str(self._state.get("mode", "sqlite")))

    def status(self) -> dict[str, Any]:
        with self._lock:
            shadow = deepcopy(self._state.get("shadow", {}))
            return {
                "mode": self.mode.value,
                "sqlite_database": str(self.database_path.resolve()) if str(self.database_path) != ":memory:" else ":memory:",
                "postgres_configured": bool(self.database_url),
                "postgres_target": describe_database_url(self.database_url),
                "repository_coverage": dict(RUNTIME_COVERAGE),
                "full_repository_coverage": self.full_repository_coverage,
                "required_shadow_passes": self.required_shadow_passes,
                "shadow": shadow,
                "last_gate": deepcopy(self._state.get("last_gate")),
                "last_transition": deepcopy(self._state.get("last_transition")),
                "startup_warning": self._state.get("startup_warning"),
            }

    def enable_shadow(self, *, confirmation: str) -> dict[str, Any]:
        if confirmation != "ENABLE_SHADOW_READS":
            raise ValueError("confirmation must be ENABLE_SHADOW_READS")
        if not self.database_url:
            raise ValueError("DATABASE_URL is required before enabling shadow reads")
        with self._lock:
            previous = self.mode.value
            self._state["mode"] = RuntimeDatabaseMode.SHADOW.value
            self._state["last_transition"] = {
                "from": previous, "to": RuntimeDatabaseMode.SHADOW.value, "at": self._now(),
            }
            self._save_state()
            return self.status()

    def record_shadow_result(
        self,
        *,
        repository: str,
        operation: str,
        matched: bool,
        differences: list[dict[str, Any]] | None = None,
        error: Exception | str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "at": self._now(),
            "repository": repository,
            "operation": operation,
            "matched": bool(matched),
            "differences": list(differences or [])[:20],
        }
        if error is not None:
            entry["error"] = redact_runtime_error(error, self.database_url)
        with self._lock:
            shadow = self._state.setdefault("shadow", self._default_state()["shadow"])
            shadow["total_checks"] = int(shadow.get("total_checks", 0)) + 1
            if matched:
                shadow["passed_checks"] = int(shadow.get("passed_checks", 0)) + 1
                shadow["consecutive_passes"] = int(shadow.get("consecutive_passes", 0)) + 1
            else:
                shadow["failed_checks"] = int(shadow.get("failed_checks", 0)) + 1
                shadow["consecutive_passes"] = 0
            recent = list(shadow.get("recent") or [])
            shadow["recent"] = [entry, *recent][:50]
            self._save_state()

    def check_cutover(self, *, credentials_reauthorized: bool = False) -> dict[str, Any]:
        checks: dict[str, dict[str, Any]] = {}
        if not self.database_url:
            checks["postgres_health"] = {"passed": False, "detail": "DATABASE_URL is not configured"}
            checks["migration_validation"] = {"passed": False, "detail": "PostgreSQL target is unavailable"}
            credential_actions = {"wordpress_reauthorization": 0, "gsc_oauth_reauthorization": 0}
        else:
            try:
                self.postgres_health_check(self.database_url)
                checks["postgres_health"] = {"passed": True}
            except Exception as error:  # noqa: BLE001 - gate must report availability safely
                checks["postgres_health"] = {"passed": False, "detail": redact_runtime_error(error, self.database_url)}
            try:
                report = self.migration_suite_factory(self.database_path, self.database_url).validate()
                credential_actions = dict(report.credential_actions)
                checks["migration_validation"] = {
                    "passed": bool(report.valid),
                    "source_count": int(report.source_count),
                    "target_count": int(report.target_count),
                    "table_count": int(getattr(report, "target_table_count", 0)),
                    "expected_table_count": int(getattr(report, "expected_table_count", 0)),
                    "foreign_key_count": int(getattr(report, "target_foreign_key_count", 0)),
                    "expected_foreign_key_count": int(getattr(report, "expected_foreign_key_count", 0)),
                    "stopped_after": getattr(report, "stopped_after", None),
                }
            except Exception as error:  # noqa: BLE001
                credential_actions = self._credential_actions()
                checks["migration_validation"] = {"passed": False, "detail": redact_runtime_error(error, self.database_url)}

        vault = self.credential_vault.readiness() if self.credential_vault is not None else None
        wordpress_needed = int(credential_actions.get("wordpress_reauthorization", 0))
        gsc_needed = int(credential_actions.get("gsc_oauth_reauthorization", 0))
        vault_ready = vault is not None and (
            int(vault.get("wordpress_credentials", 0)) >= wordpress_needed
            and int(vault.get("gsc_oauth_credentials", 0)) >= gsc_needed
        )
        credentials_needed = wordpress_needed + gsc_needed > 0
        checks["credentials_reauthorized"] = {
            "passed": not credentials_needed or vault_ready or bool(credentials_reauthorized),
            "required": credential_actions,
            "stored_in_local_vault": {
                "wordpress_credentials": int((vault or {}).get("wordpress_credentials", 0)),
                "gsc_oauth_credentials": int((vault or {}).get("gsc_oauth_credentials", 0)),
            },
        }
        checks["credential_vault"] = {
            "passed": self.credential_vault is not None,
            "ready": self.credential_vault is not None,
        }
        unfinished = self._unfinished_sqlite_tasks()
        checks["no_unfinished_sqlite_tasks"] = {"passed": unfinished == 0, "count": unfinished}
        consecutive = int(self._state.get("shadow", {}).get("consecutive_passes", 0))
        checks["shadow_reads"] = {
            "passed": consecutive >= self.required_shadow_passes,
            "consecutive_passes": consecutive,
            "required": self.required_shadow_passes,
        }
        uncovered = [] if self.full_repository_coverage else sorted(
            name for name, covered in RUNTIME_COVERAGE.items() if not covered
        ) or (["runtime_coverage_not_confirmed"] if not self.full_repository_coverage else [])
        checks["full_repository_coverage"] = {
            "passed": self.full_repository_coverage,
            "uncovered": uncovered,
            "detail": "direct SQLite modules must be repository-migrated before a full runtime cutover" if uncovered else None,
        }
        ready = all(bool(item.get("passed")) for item in checks.values())
        result = {"ready": ready, "checked_at": self._now(), "checks": checks}
        with self._lock:
            self._state["last_gate"] = result
            self._save_state()
        return deepcopy(result)

    def cutover(self, *, confirmation: str, credentials_reauthorized: bool = False) -> dict[str, Any]:
        if confirmation != "CUTOVER_TO_POSTGRES":
            raise ValueError("confirmation must be CUTOVER_TO_POSTGRES")
        gate = self.check_cutover(credentials_reauthorized=credentials_reauthorized)
        if not gate["ready"]:
            raise RuntimeError("PostgreSQL cutover gate is not ready")
        with self._lock:
            previous = self.mode.value
            self._state["mode"] = RuntimeDatabaseMode.POSTGRES.value
            self._state["last_transition"] = {
                "from": previous, "to": RuntimeDatabaseMode.POSTGRES.value, "at": self._now(),
            }
            self._save_state()
            return self.status()

    def rollback(self, *, confirmation: str) -> dict[str, Any]:
        if confirmation != "ROLLBACK_TO_SQLITE":
            raise ValueError("confirmation must be ROLLBACK_TO_SQLITE")
        with self._lock:
            previous = self.mode.value
            self._state["mode"] = RuntimeDatabaseMode.SQLITE.value
            self._state["last_transition"] = {
                "from": previous, "to": RuntimeDatabaseMode.SQLITE.value, "at": self._now(),
                "postgres_data_retained": True,
            }
            self._save_state()
            return self.status()

    def _credential_actions(self) -> dict[str, int]:
        connection = initialize_database(self.database_path)
        try:
            return {
                "wordpress_reauthorization": int(connection.execute(
                    "SELECT COUNT(*) FROM project_wordpress_configs WHERE trim(COALESCE(application_password,''))<>''"
                ).fetchone()[0]),
                "gsc_oauth_reauthorization": int(connection.execute(
                    "SELECT COUNT(*) FROM gsc_oauth_connection WHERE trim(COALESCE(refresh_token,''))<>''"
                ).fetchone()[0]),
            }
        finally:
            connection.close()

    def _unfinished_sqlite_tasks(self) -> int:
        connection = initialize_database(self.database_path)
        try:
            durable = int(connection.execute(
                "SELECT COUNT(*) FROM durable_task_queue WHERE status IN ('queued','running','retry_wait')"
            ).fetchone()[0])
            agent = int(connection.execute(
                "SELECT COUNT(*) FROM agent_jobs WHERE status IN ('queued','planning','running','retrying','paused','awaiting_approval')"
            ).fetchone()[0])
            return durable + agent
        finally:
            connection.close()

    @staticmethod
    def _default_postgres_health_check(database_url: str) -> None:
        try:
            from psycopg import connect
        except ImportError as error:  # pragma: no cover - production dependency
            raise RuntimeError("install requirements-postgres.txt before PostgreSQL runtime checks") from error
        with connect(database_url, connect_timeout=5) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone()[0] != 1:
                raise RuntimeError("PostgreSQL health query returned an unexpected value")
