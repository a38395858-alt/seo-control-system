"""Reusable snapshot migration primitives for staged PostgreSQL adoption."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


class SnapshotRepository(Protocol):
    def list_migration_snapshots(self) -> list[dict[str, Any]]: ...
    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class SnapshotMigrationReport:
    source_count: int
    target_count: int
    migrated_count: int
    missing_keys: tuple[str, ...]
    extra_keys: tuple[str, ...]
    mismatches: tuple[dict[str, Any], ...]
    applied: bool

    @property
    def valid(self) -> bool:
        return (
            self.source_count == self.target_count
            and not self.missing_keys
            and not self.extra_keys
            and not self.mismatches
        )

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "valid": self.valid}


class SnapshotMigrationService:
    """Copy idempotent snapshots and compare their normalized business fields."""

    def __init__(
        self,
        source: SnapshotRepository,
        target: SnapshotRepository,
        *,
        key: Callable[[Mapping[str, Any]], str],
        normalize: Callable[[Mapping[str, Any]], dict[str, Any]],
    ) -> None:
        self.source = source
        self.target = target
        self.key = key
        self.normalize = normalize

    def migrate(self, *, apply: bool = False) -> SnapshotMigrationReport:
        source_rows = self.source.list_migration_snapshots()
        if not apply:
            return SnapshotMigrationReport(len(source_rows), 0, 0, (), (), (), False)
        batch_upsert = getattr(self.target, "upsert_migration_snapshots", None)
        if callable(batch_upsert):
            batch_upsert(source_rows)
        else:
            for row in source_rows:
                self.target.upsert_migration_snapshot(row)
        reset = getattr(self.target, "reset_identity", None)
        if callable(reset):
            reset()
        return self.validate(migrated_count=len(source_rows), applied=True)

    def validate(self, *, migrated_count: int = 0, applied: bool = False) -> SnapshotMigrationReport:
        source = {
            self.key(row): self.normalize(row)
            for row in self.source.list_migration_snapshots()
        }
        target = {
            self.key(row): self.normalize(row)
            for row in self.target.list_migration_snapshots()
        }
        missing = tuple(sorted(source.keys() - target.keys()))
        extra = tuple(sorted(target.keys() - source.keys()))
        mismatches: list[dict[str, Any]] = []
        for snapshot_key in sorted(source.keys() & target.keys()):
            fields = {
                field: {"source": source[snapshot_key].get(field), "target": target[snapshot_key].get(field)}
                for field in sorted(source[snapshot_key].keys() | target[snapshot_key].keys())
                if source[snapshot_key].get(field) != target[snapshot_key].get(field)
            }
            if fields:
                mismatches.append({"key": snapshot_key, "fields": fields})
        return SnapshotMigrationReport(
            len(source), len(target), migrated_count, missing, extra, tuple(mismatches), applied
        )
