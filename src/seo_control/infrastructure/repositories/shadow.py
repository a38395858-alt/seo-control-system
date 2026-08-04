"""SQLite-primary repositories with redacted PostgreSQL shadow comparisons."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from seo_control.infrastructure.runtime_database import RuntimeDatabaseController, RuntimeDatabaseMode


def _normal(value: Any) -> Any:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        candidate = value.strip().replace(" ", "T")
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _normal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normal(item) for item in value]
    return value


def compare_rows(source: Sequence[Mapping[str, Any]], target: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Describe only record identities and field names; never emit field values."""
    def key(row: Mapping[str, Any], index: int) -> str:
        for fields in (("id",), ("project_id", "normalized_url"), ("project_id", "source_type", "source_value")):
            if all(field in row for field in fields):
                return ":".join(str(row.get(field)) for field in fields)
        return f"row:{index}"

    left = {key(row, index): row for index, row in enumerate(source)}
    right = {key(row, index): row for index, row in enumerate(target)}
    differences: list[dict[str, Any]] = []
    for missing in sorted(set(left) - set(right)):
        differences.append({"key": missing, "kind": "missing_in_postgres"})
    for extra in sorted(set(right) - set(left)):
        differences.append({"key": extra, "kind": "extra_in_postgres"})
    for identity in sorted(set(left) & set(right)):
        fields = sorted(
            field for field in set(left[identity]) | set(right[identity])
            if _normal(left[identity].get(field)) != _normal(right[identity].get(field))
        )
        if fields:
            differences.append({"key": identity, "kind": "field_mismatch", "fields": fields})
    return differences[:20]


class _ShadowRepository:
    repository_name = "repository"

    def __init__(self, sqlite_repository: Any, postgres_repository: Any | None, controller: RuntimeDatabaseController) -> None:
        self.sqlite = sqlite_repository
        self.postgres = postgres_repository
        self.controller = controller

    def _read(self, operation: str, function: Callable[[Any], Any]) -> Any:
        mode = self.controller.mode
        if mode is RuntimeDatabaseMode.POSTGRES:
            if self.postgres is None:
                raise RuntimeError("PostgreSQL repository is not configured")
            return function(self.postgres)
        source = function(self.sqlite)
        if mode is RuntimeDatabaseMode.SHADOW:
            try:
                if self.postgres is None:
                    raise RuntimeError("PostgreSQL repository is not configured")
                target = function(self.postgres)
                source_rows = source if isinstance(source, list) else [source]
                target_rows = target if isinstance(target, list) else [target]
                differences = compare_rows(source_rows, target_rows)
                self.controller.record_shadow_result(
                    repository=self.repository_name, operation=operation,
                    matched=not differences, differences=differences,
                )
            except Exception as error:  # noqa: BLE001 - shadow failures never fail primary requests
                self.controller.record_shadow_result(
                    repository=self.repository_name, operation=operation, matched=False, error=error,
                )
        return source

    def _write(self, operation: str, function: Callable[[Any], Any]) -> Any:
        if self.controller.mode is RuntimeDatabaseMode.POSTGRES:
            if self.postgres is None:
                raise RuntimeError("PostgreSQL repository is not configured")
            return function(self.postgres)
        # Shadow mode is intentionally read-only toward PostgreSQL.  Writes
        # remain atomic in SQLite until the final incremental-sync phase.
        return function(self.sqlite)


class ShadowProjectRepository(_ShadowRepository):
    repository_name = "projects"

    def create_project(self, **fields: Any) -> dict[str, Any]:
        return self._write("create_project", lambda repository: repository.create_project(**fields))

    def list_projects(self) -> list[dict[str, Any]]:
        return self._read("list_projects", lambda repository: repository.list_projects())

    def list_project_summaries(self) -> list[dict[str, Any]]:
        return self._read("list_project_summaries", lambda repository: repository.list_project_summaries())

    def update_project(self, project_id: int, fields: Mapping[str, str]) -> dict[str, Any]:
        return self._write("update_project", lambda repository: repository.update_project(project_id, fields))

    def delete_project(self, project_id: int) -> None:
        return self._write("delete_project", lambda repository: repository.delete_project(project_id))

    def upsert_project_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        return self._write("upsert_project_snapshot", lambda repository: repository.upsert_project_snapshot(snapshot))


class ShadowKeywordRepository(_ShadowRepository):
    repository_name = "keywords"

    def list_keywords(self, project_id: int) -> list[dict[str, Any]]:
        return self._read("list_keywords", lambda repository: repository.list_keywords(project_id))

    def soft_delete(self, project_id: int, *, keyword_ids: Sequence[int] = (), clear_all: bool = False) -> int:
        return self._write(
            "soft_delete",
            lambda repository: repository.soft_delete(project_id, keyword_ids=keyword_ids, clear_all=clear_all),
        )


class ShadowCollectionRepository(_ShadowRepository):
    repository_name = "collection"

    def list_collection_plans(self, project_id: int) -> list[dict[str, Any]]:
        return self._read("list_collection_plans", lambda repository: repository.list_collection_plans(project_id))

    def list_catalog(self, project_id: int) -> list[dict[str, Any]]:
        return self._read("list_catalog", lambda repository: repository.list_catalog(project_id))

