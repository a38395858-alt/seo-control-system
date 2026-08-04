"""Safe orchestration and aggregate reporting for the staged PostgreSQL migration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Sequence
from urllib.parse import unquote, urlparse

from seo_control.infrastructure.database import initialize_database

from .collection import CollectionMigrationService, PostgresCollectionRepository, SQLiteCollectionRepository
from .evidence import EvidenceMigrationService, PostgresEvidenceRepository, SQLiteEvidenceRepository
from .keywords import KeywordMigrationService, PostgresKeywordRepository, SQLiteKeywordRepository
from .legacy import LegacyMigrationService, PostgresLegacyRepository, SQLiteLegacyRepository
from .projects import PostgresProjectRepository, ProjectMigrationService, SQLiteProjectRepository
from .workflow import PostgresWorkflowRepository, SQLiteWorkflowRepository, WorkflowMigrationService


@dataclass(frozen=True)
class MigrationBatchDefinition:
    name: str
    source_factory: Callable[[str | Path], Any]
    target_factory: Callable[[str], Any]
    service_factory: Callable[[Any, Any], Any]
    list_source: Callable[[Any], list[dict[str, Any]]]


DEFAULT_MIGRATION_BATCHES: tuple[MigrationBatchDefinition, ...] = (
    MigrationBatchDefinition(
        "projects", SQLiteProjectRepository, PostgresProjectRepository, ProjectMigrationService,
        lambda repository: repository.list_project_summaries(),
    ),
    MigrationBatchDefinition(
        "keywords", SQLiteKeywordRepository, PostgresKeywordRepository, KeywordMigrationService,
        lambda repository: repository.list_migration_snapshots(),
    ),
    MigrationBatchDefinition(
        "collection", SQLiteCollectionRepository, PostgresCollectionRepository, CollectionMigrationService,
        lambda repository: repository.list_migration_snapshots(),
    ),
    MigrationBatchDefinition(
        "evidence", SQLiteEvidenceRepository, PostgresEvidenceRepository, EvidenceMigrationService,
        lambda repository: repository.list_migration_snapshots(),
    ),
    MigrationBatchDefinition(
        "workflow", SQLiteWorkflowRepository, PostgresWorkflowRepository, WorkflowMigrationService,
        lambda repository: repository.list_migration_snapshots(),
    ),
    MigrationBatchDefinition(
        "legacy", SQLiteLegacyRepository, PostgresLegacyRepository, LegacyMigrationService,
        lambda repository: repository.list_migration_snapshots(),
    ),
)


@dataclass(frozen=True)
class MigrationSuiteReport:
    mode: str
    source_database: str
    target: dict[str, Any] | None
    batches: tuple[dict[str, Any], ...]
    source_count: int
    target_count: int
    migrated_count: int
    credential_actions: dict[str, int]
    stopped_after: str | None
    valid: bool | None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class PostgresMigrationSuite:
    """Run all batches in dependency order and emit a value-redacted report."""

    def __init__(
        self,
        database_path: str | Path,
        database_url: str | None = None,
        *,
        definitions: Sequence[MigrationBatchDefinition] = DEFAULT_MIGRATION_BATCHES,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_url = database_url
        self.definitions = tuple(definitions)

    def preview(self) -> MigrationSuiteReport:
        batches: list[dict[str, Any]] = []
        total = 0
        for definition in self.definitions:
            count = len(definition.list_source(definition.source_factory(self.database_path)))
            total += count
            batches.append({"name": definition.name, "status": "preview", "source_count": count})
        return MigrationSuiteReport(
            mode="preview",
            source_database=str(self.database_path.resolve()),
            target=_target_descriptor(self.database_url),
            batches=tuple(batches),
            source_count=total,
            target_count=0,
            migrated_count=0,
            credential_actions=self._credential_actions(),
            stopped_after=None,
            valid=None,
        )

    def migrate(self, *, apply: bool = False, confirmed: bool = False) -> MigrationSuiteReport:
        if not apply:
            return self.preview()
        if not confirmed:
            raise ValueError("full migration apply requires explicit confirmation")
        return self._execute(mode="apply")

    def validate(self) -> MigrationSuiteReport:
        return self._execute(mode="validate")

    def _execute(self, *, mode: str) -> MigrationSuiteReport:
        if not self.database_url:
            raise ValueError("PostgreSQL database URL is required")
        batch_results: list[dict[str, Any]] = []
        total_source = total_target = total_migrated = 0
        stopped_after: str | None = None
        all_valid = True
        for index, definition in enumerate(self.definitions):
            try:
                source = definition.source_factory(self.database_path)
                target = definition.target_factory(self.database_url)
                service = definition.service_factory(source, target)
                report = service.migrate(apply=True) if mode == "apply" else service.validate()
                safe = _safe_batch_report(definition.name, report)
            except Exception as error:  # noqa: BLE001 - migration must stop and report the failed batch
                safe = {
                    "name": definition.name,
                    "status": "failed",
                    "valid": False,
                    "error": _safe_error(error, self.database_url),
                }
            batch_results.append(safe)
            total_source += int(safe.get("source_count", 0))
            total_target += int(safe.get("target_count", 0))
            total_migrated += int(safe.get("migrated_count", 0))
            if not safe.get("valid", False):
                all_valid = False
                stopped_after = definition.name
                for pending in self.definitions[index + 1:]:
                    batch_results.append({"name": pending.name, "status": "not_run", "valid": False})
                break
        return MigrationSuiteReport(
            mode=mode,
            source_database=str(self.database_path.resolve()),
            target=_target_descriptor(self.database_url),
            batches=tuple(batch_results),
            source_count=total_source,
            target_count=total_target,
            migrated_count=total_migrated,
            credential_actions=self._credential_actions(),
            stopped_after=stopped_after,
            valid=all_valid and len(batch_results) == len(self.definitions),
        )

    def _credential_actions(self) -> dict[str, int]:
        connection = initialize_database(self.database_path)
        try:
            wordpress = int(connection.execute(
                "SELECT COUNT(*) FROM project_wordpress_configs "
                "WHERE trim(COALESCE(application_password,''))<>''"
            ).fetchone()[0])
            gsc = int(connection.execute(
                "SELECT COUNT(*) FROM gsc_oauth_connection "
                "WHERE trim(COALESCE(refresh_token,''))<>''"
            ).fetchone()[0])
            return {"wordpress_reauthorization": wordpress, "gsc_oauth_reauthorization": gsc}
        finally:
            connection.close()


def _safe_batch_report(name: str, report: Any) -> dict[str, Any]:
    value = report.as_dict()
    missing = value.get("missing_keys", value.get("missing_ids", ()))
    extra = value.get("extra_keys", value.get("extra_ids", ()))
    mismatches = value.get("mismatches", ())
    return {
        "name": name,
        "status": "completed" if value.get("valid") else "invalid",
        "source_count": int(value.get("source_count", 0)),
        "target_count": int(value.get("target_count", 0)),
        "migrated_count": int(value.get("migrated_count", 0)),
        "missing": list(missing),
        "extra": list(extra),
        "mismatches": [
            {
                "key": mismatch.get("key", mismatch.get("project_id")),
                "fields": sorted((mismatch.get("fields") or {}).keys()),
            }
            for mismatch in mismatches
        ],
        "valid": bool(value.get("valid")),
    }


def _target_descriptor(database_url: str | None) -> dict[str, Any] | None:
    if not database_url:
        return None
    parsed = urlparse(database_url)
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "database": unquote(parsed.path.lstrip("/")),
    }


def _safe_error(error: Exception, database_url: str) -> str:
    text = str(error).replace(database_url, "[DATABASE_URL]")
    text = re.sub(r"(?i)(password\s*=\s*)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(postgres(?:ql)?://)[^@\s]+@", r"\1[REDACTED]@", text)
    return text[:1000]
