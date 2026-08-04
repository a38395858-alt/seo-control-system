"""Exact-schema PostgreSQL runtime used after the curated migration mirror.

P6.2-P6.7 created value-governed ``workspace_*`` migration tables.  The live
workspace also needs every existing CRUD statement to keep its transaction
semantics while it is moved away from SQLite.  This module builds a dedicated,
isolated PostgreSQL schema from SQLite metadata and copies all non-secret rows.

The schema is deliberately separate from ``public`` and from the curated
migration mirror.  It can be validated or discarded without touching either
the SQLite fallback or another application's PostgreSQL tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Iterator, Mapping as MappingABC
import re
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

from seo_control.infrastructure.database import initialize_database


DEFAULT_RUNTIME_SCHEMA = "seo_workspace_runtime"
EXCLUDED_TABLES = frozenset({"schema_migrations"})
SECRET_COLUMNS = {
    "project_wordpress_configs": frozenset({"application_password"}),
    "gsc_oauth_connection": frozenset({"refresh_token"}),
}
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return value


def _quoted(value: str) -> str:
    return '"' + _identifier(value).replace('"', '""') + '"'


@dataclass(frozen=True)
class RuntimeColumn:
    name: str
    sqlite_type: str
    not_null: bool
    default: str | None
    primary_position: int

    @property
    def postgres_type(self) -> str:
        value = self.sqlite_type.upper()
        if "INT" in value:
            return "BIGINT"
        if any(marker in value for marker in ("REAL", "FLOA", "DOUB")):
            return "DOUBLE PRECISION"
        if "BLOB" in value:
            return "BYTEA"
        return "TEXT"


@dataclass(frozen=True)
class RuntimeIndex:
    name: str
    columns: tuple[str, ...]
    unique: bool
    predicate: str | None = None


@dataclass(frozen=True)
class RuntimeForeignKey:
    parent_table: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]
    on_delete: str


@dataclass(frozen=True)
class RuntimeTable:
    name: str
    columns: tuple[RuntimeColumn, ...]
    indexes: tuple[RuntimeIndex, ...]
    foreign_keys: tuple[RuntimeForeignKey, ...]

    @property
    def primary_key(self) -> tuple[str, ...]:
        return tuple(
            column.name for column in sorted(self.columns, key=lambda item: item.primary_position)
            if column.primary_position
        )

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


@dataclass(frozen=True)
class RuntimePostgresReport:
    mode: str
    schema: str
    source_count: int
    target_count: int
    migrated_count: int
    tables: tuple[dict[str, Any], ...]
    credential_actions: dict[str, int]
    valid: bool | None
    expected_table_count: int = 0
    target_table_count: int = 0
    expected_foreign_key_count: int = 0
    target_foreign_key_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def inspect_sqlite_runtime_schema(database_path: str | Path) -> tuple[RuntimeTable, ...]:
    connection = initialize_database(database_path)
    try:
        names = [
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            if str(row[0]) not in EXCLUDED_TABLES
        ]
        tables: list[RuntimeTable] = []
        for name in names:
            _identifier(name)
            columns = tuple(
                RuntimeColumn(
                    name=str(row[1]), sqlite_type=str(row[2] or "TEXT"),
                    not_null=bool(row[3]), default=None if row[4] is None else str(row[4]),
                    primary_position=int(row[5] or 0),
                )
                for row in connection.execute(f"PRAGMA table_info({_quoted(name)})").fetchall()
            )
            index_sql = {
                str(row[0]): str(row[1] or "")
                for row in connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name=?", (name,)
                ).fetchall()
            }
            indexes: list[RuntimeIndex] = []
            for row in connection.execute(f"PRAGMA index_list({_quoted(name)})").fetchall():
                index_name, unique = str(row[1]), bool(row[2])
                # Primary-key auto indexes are already represented in CREATE TABLE.
                if str(row[3] or "") == "pk":
                    continue
                fields = tuple(
                    str(item[2]) for item in connection.execute(f"PRAGMA index_info({_quoted(index_name)})").fetchall()
                    if item[2] is not None
                )
                if not fields:
                    continue
                sql = index_sql.get(index_name, "")
                match = re.search(r"\bWHERE\b(.+)$", sql, flags=re.IGNORECASE | re.DOTALL)
                indexes.append(RuntimeIndex(index_name, fields, unique, match.group(1).strip() if match else None))
            foreign_groups: dict[int, list[tuple[Any, ...]]] = {}
            for row in connection.execute(f"PRAGMA foreign_key_list({_quoted(name)})").fetchall():
                foreign_groups.setdefault(int(row[0]), []).append(tuple(row))
            foreign_keys = tuple(
                RuntimeForeignKey(
                    parent_table=str(sorted(rows, key=lambda item: int(item[1]))[0][2]),
                    from_columns=tuple(str(item[3]) for item in sorted(rows, key=lambda item: int(item[1]))),
                    to_columns=tuple(str(item[4]) for item in sorted(rows, key=lambda item: int(item[1]))),
                    on_delete=str(sorted(rows, key=lambda item: int(item[1]))[0][6] or "NO ACTION").upper(),
                )
                for _foreign_id, rows in sorted(foreign_groups.items())
            )
            tables.append(RuntimeTable(name, columns, tuple(indexes), foreign_keys))
        return tuple(tables)
    finally:
        connection.close()


def _postgres_default(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.upper() == "CURRENT_TIMESTAMP":
        # Runtime columns intentionally remain TEXT-compatible so existing
        # timestamp comparisons and JSON payloads retain their representation.
        return "to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')"
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", normalized):
        return normalized
    if normalized.startswith("'") and normalized.endswith("'"):
        return normalized
    return None


def runtime_table_ddl(table: RuntimeTable, schema: str = DEFAULT_RUNTIME_SCHEMA) -> str:
    schema, name = _identifier(schema), _identifier(table.name)
    primary = table.primary_key
    definitions: list[str] = []
    for column in table.columns:
        definition = f"{_quoted(column.name)} {column.postgres_type}"
        if column.not_null or column.primary_position:
            definition += " NOT NULL"
        default = _postgres_default(column.default)
        if default is not None:
            definition += f" DEFAULT {default}"
        definitions.append(definition)
    if primary:
        definitions.append("PRIMARY KEY(" + ",".join(_quoted(column) for column in primary) + ")")
    return f"CREATE TABLE IF NOT EXISTS {_quoted(schema)}.{_quoted(name)} ({','.join(definitions)})"


def runtime_index_ddl(table: RuntimeTable, index: RuntimeIndex, schema: str = DEFAULT_RUNTIME_SCHEMA) -> str:
    schema = _identifier(schema)
    generated_name = _identifier(f"rt_{table.name}_{index.name}"[:60])
    unique = "UNIQUE " if index.unique else ""
    predicate = f" WHERE {index.predicate}" if index.predicate else ""
    return (
        f"CREATE {unique}INDEX IF NOT EXISTS {_quoted(generated_name)} "
        f"ON {_quoted(schema)}.{_quoted(table.name)}({','.join(_quoted(column) for column in index.columns)})"
        f"{predicate}"
    )


class PostgresRuntimeMigration:
    """Create, synchronize, and field-validate the isolated runtime schema."""

    def __init__(
        self,
        database_path: str | Path,
        database_url: str,
        *,
        schema: str = DEFAULT_RUNTIME_SCHEMA,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not str(database_url).strip():
            raise ValueError("PostgreSQL database URL is required")
        self.database_path = Path(database_path)
        self.database_url = database_url
        self.schema = _identifier(schema)
        self.connection_factory = connection_factory
        self.tables = inspect_sqlite_runtime_schema(database_path)

    def _connect(self):
        if self.connection_factory is not None:
            return self.connection_factory(self.database_url)
        try:
            from psycopg import connect
            from psycopg.rows import dict_row
        except ImportError as error:  # pragma: no cover - production dependency
            raise RuntimeError("install requirements-postgres.txt before PostgreSQL runtime migration") from error
        return connect(self.database_url, row_factory=dict_row)

    def preview(self) -> RuntimePostgresReport:
        source = initialize_database(self.database_path)
        try:
            rows = []
            total = 0
            for table in self.tables:
                count = int(source.execute(f"SELECT COUNT(*) FROM {_quoted(table.name)}").fetchone()[0])
                if table.name == "gsc_oauth_connection":
                    count = 0
                total += count
                rows.append({"name": table.name, "source_count": count, "status": "preview"})
            return RuntimePostgresReport(
                "preview", self.schema, total, 0, 0, tuple(rows), self._credential_actions(source), None,
                len(self.tables), 0, sum(len(table.foreign_keys) for table in self.tables), 0,
            )
        finally:
            source.close()

    def apply(self, *, confirmed: bool = False) -> RuntimePostgresReport:
        if not confirmed:
            raise ValueError("runtime PostgreSQL synchronization requires explicit confirmation")
        source = initialize_database(self.database_path)
        source.row_factory = sqlite3.Row
        table_reports: list[dict[str, Any]] = []
        total_source = total_target = total_migrated = 0
        try:
            with self._connect() as target, target.cursor() as cursor:
                cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {_quoted(self.schema)}")
                for table in self.tables:
                    cursor.execute(runtime_table_ddl(table, self.schema))
                    for index in table.indexes:
                        cursor.execute(runtime_index_ddl(table, index, self.schema))
                ordered = self._dependency_order()
                migrated_counts: dict[str, int] = {}
                for table in ordered:
                    migrated_counts[table.name] = self._upsert_table(source, cursor, table)
                for table in reversed(ordered):
                    self._delete_extra_rows(source, cursor, table)
                    self._reset_sequence(cursor, table)
                for table in ordered:
                    self._ensure_foreign_keys(cursor, table)
                for table in self.tables:
                    checked = self._validate_table(source, cursor, table)
                    report = {
                        **checked,
                        "migrated_count": migrated_counts[table.name],
                        "status": "completed" if checked["valid"] else "invalid",
                    }
                    table_reports.append(report)
                    total_source += report["source_count"]
                    total_target += report["target_count"]
                    total_migrated += report["migrated_count"]
                target_tables, target_foreign_keys = self._schema_counts(cursor)
            expected_foreign_keys = sum(len(table.foreign_keys) for table in self.tables)
            return RuntimePostgresReport(
                "apply", self.schema, total_source, total_target, total_migrated,
                tuple(table_reports), self._credential_actions(source),
                all(item["valid"] for item in table_reports)
                and target_tables == len(self.tables)
                and target_foreign_keys == expected_foreign_keys,
                len(self.tables), target_tables, expected_foreign_keys, target_foreign_keys,
            )
        finally:
            source.close()

    def validate(self) -> RuntimePostgresReport:
        source = initialize_database(self.database_path)
        source.row_factory = sqlite3.Row
        table_reports: list[dict[str, Any]] = []
        total_source = total_target = 0
        try:
            with self._connect() as target, target.cursor() as cursor:
                for table in self.tables:
                    report = self._validate_table(source, cursor, table)
                    table_reports.append(report)
                    total_source += report["source_count"]
                    total_target += report["target_count"]
                target_tables, target_foreign_keys = self._schema_counts(cursor)
            expected_foreign_keys = sum(len(table.foreign_keys) for table in self.tables)
            return RuntimePostgresReport(
                "validate", self.schema, total_source, total_target, 0,
                tuple(table_reports), self._credential_actions(source),
                all(item["valid"] for item in table_reports)
                and target_tables == len(self.tables)
                and target_foreign_keys == expected_foreign_keys,
                len(self.tables), target_tables, expected_foreign_keys, target_foreign_keys,
            )
        finally:
            source.close()

    def _source_rows(self, source: sqlite3.Connection, table: RuntimeTable) -> list[dict[str, Any]]:
        if table.name == "gsc_oauth_connection":
            return []
        rows = [dict(row) for row in source.execute(f"SELECT * FROM {_quoted(table.name)}").fetchall()]
        secrets = SECRET_COLUMNS.get(table.name, frozenset())
        if secrets:
            for row in rows:
                for column in secrets:
                    row[column] = ""
        return rows

    def _upsert_table(self, source: sqlite3.Connection, cursor: Any, table: RuntimeTable) -> int:
        rows = self._source_rows(source, table)
        columns, primary = table.column_names, table.primary_key
        placeholders = ",".join("%s" for _column in columns)
        updates = [column for column in columns if column not in primary]
        conflict = (
            f" ON CONFLICT({','.join(_quoted(column) for column in primary)}) DO UPDATE SET "
            + ",".join(f"{_quoted(column)}=excluded.{_quoted(column)}" for column in updates)
            if updates else f" ON CONFLICT({','.join(_quoted(column) for column in primary)}) DO NOTHING"
        )
        statement = (
            f"INSERT INTO {_quoted(self.schema)}.{_quoted(table.name)}"
            f"({','.join(_quoted(column) for column in columns)}) VALUES({placeholders}){conflict}"
        )
        for row in rows:
            cursor.execute(statement, tuple(row.get(column) for column in columns))
        return len(rows)

    def _delete_extra_rows(self, source: sqlite3.Connection, cursor: Any, table: RuntimeTable) -> None:
        rows = self._source_rows(source, table)
        primary = table.primary_key
        source_keys = {tuple(row.get(column) for column in primary) for row in rows}
        cursor.execute(
            f"SELECT {','.join(_quoted(column) for column in primary)} FROM {_quoted(self.schema)}.{_quoted(table.name)}"
        )
        for target_row in cursor.fetchall():
            key = tuple(target_row[column] for column in primary)
            if key in source_keys:
                continue
            cursor.execute(
                f"DELETE FROM {_quoted(self.schema)}.{_quoted(table.name)} WHERE "
                + " AND ".join(f"{_quoted(column)}=%s" for column in primary), key,
            )

    def _dependency_order(self) -> list[RuntimeTable]:
        by_name = {table.name: table for table in self.tables}
        ordered: list[RuntimeTable] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(table: RuntimeTable) -> None:
            if table.name in visited:
                return
            if table.name in visiting:
                # SQLite contains no required cyclic business relationship;
                # keep deterministic order if a future optional cycle appears.
                return
            visiting.add(table.name)
            for foreign in table.foreign_keys:
                parent = by_name.get(foreign.parent_table)
                if parent is not None:
                    visit(parent)
            visiting.remove(table.name)
            visited.add(table.name)
            ordered.append(table)

        for table in self.tables:
            visit(table)
        return ordered

    def _ensure_foreign_keys(self, cursor: Any, table: RuntimeTable) -> None:
        for position, foreign in enumerate(table.foreign_keys):
            constraint = _identifier(f"rtfk_{table.name}_{position}"[:60])
            cursor.execute(
                "SELECT 1 FROM pg_constraint AS constraints "
                "JOIN pg_class AS tables ON tables.oid=constraints.conrelid "
                "JOIN pg_namespace AS schemas ON schemas.oid=tables.relnamespace "
                "WHERE constraints.conname=%s AND schemas.nspname=%s AND tables.relname=%s",
                (constraint, self.schema, table.name),
            )
            if cursor.fetchone() is not None:
                continue
            delete_rule = foreign.on_delete if foreign.on_delete in {"CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT", "NO ACTION"} else "NO ACTION"
            cursor.execute(
                f"ALTER TABLE {_quoted(self.schema)}.{_quoted(table.name)} "
                f"ADD CONSTRAINT {_quoted(constraint)} FOREIGN KEY "
                f"({','.join(_quoted(column) for column in foreign.from_columns)}) "
                f"REFERENCES {_quoted(self.schema)}.{_quoted(foreign.parent_table)}"
                f"({','.join(_quoted(column) for column in foreign.to_columns)}) "
                f"ON DELETE {delete_rule} DEFERRABLE INITIALLY DEFERRED"
            )

    def _schema_counts(self, cursor: Any) -> tuple[int, int]:
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE'",
            (self.schema,),
        )
        table_count = self._scalar_count(cursor.fetchone())
        cursor.execute(
            "SELECT COUNT(*) FROM pg_constraint AS constraints "
            "JOIN pg_class AS tables ON tables.oid=constraints.conrelid "
            "JOIN pg_namespace AS schemas ON schemas.oid=tables.relnamespace "
            "WHERE constraints.contype='f' AND constraints.conname LIKE 'rtfk_%%' "
            "AND schemas.nspname=%s",
            (self.schema,),
        )
        return table_count, self._scalar_count(cursor.fetchone())

    @staticmethod
    def _scalar_count(row: Any) -> int:
        if isinstance(row, MappingABC):
            return int(next(iter(row.values())))
        return int(row[0])

    def _validate_table(self, source: sqlite3.Connection, cursor: Any, table: RuntimeTable) -> dict[str, Any]:
        source_rows = self._source_rows(source, table)
        cursor.execute(
            f"SELECT {','.join(_quoted(column) for column in table.column_names)} "
            f"FROM {_quoted(self.schema)}.{_quoted(table.name)}"
        )
        target_rows = [dict(row) for row in cursor.fetchall()]
        primary = table.primary_key
        left = {tuple(row.get(column) for column in primary): row for row in source_rows}
        right = {tuple(row.get(column) for column in primary): row for row in target_rows}
        missing = sorted(set(left) - set(right), key=repr)
        extra = sorted(set(right) - set(left), key=repr)
        mismatches: list[dict[str, Any]] = []
        for key in sorted(set(left) & set(right), key=repr):
            fields = sorted(
                column for column in table.column_names
                if _comparable(left[key].get(column)) != _comparable(right[key].get(column))
            )
            if fields:
                mismatches.append({"key": _safe_key(key), "fields": fields})
        valid = not missing and not extra and not mismatches
        return {
            "name": table.name,
            "source_count": len(source_rows),
            "target_count": len(target_rows),
            "missing": [_safe_key(key) for key in missing[:20]],
            "extra": [_safe_key(key) for key in extra[:20]],
            "mismatches": mismatches[:20],
            "valid": valid,
        }

    def _reset_sequence(self, cursor: Any, table: RuntimeTable) -> None:
        if table.primary_key != ("id",):
            return
        sequence = _identifier(f"rt_{table.name}_id_seq"[:60])
        cursor.execute(f"CREATE SEQUENCE IF NOT EXISTS {_quoted(self.schema)}.{_quoted(sequence)}")
        cursor.execute(
            f"ALTER TABLE {_quoted(self.schema)}.{_quoted(table.name)} ALTER COLUMN id "
            f"SET DEFAULT nextval('{self.schema}.{sequence}'::regclass)"
        )
        cursor.execute(
            f"SELECT setval('{self.schema}.{sequence}'::regclass,"
            f"GREATEST(COALESCE(MAX(id),1),1),COALESCE(MAX(id),0)>0) "
            f"FROM {_quoted(self.schema)}.{_quoted(table.name)}"
        )

    @staticmethod
    def _credential_actions(source: sqlite3.Connection) -> dict[str, int]:
        return {
            "wordpress_reauthorization": int(source.execute(
                "SELECT COUNT(*) FROM project_wordpress_configs WHERE trim(COALESCE(application_password,''))<>''"
            ).fetchone()[0]),
            "gsc_oauth_reauthorization": int(source.execute(
                "SELECT COUNT(*) FROM gsc_oauth_connection WHERE trim(COALESCE(refresh_token,''))<>''"
            ).fetchone()[0]),
        }


def _comparable(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, bytes):
        return bytes(value)
    return value


def _safe_key(key: Sequence[Any]) -> str:
    return ":".join(str(value)[:80] for value in key)


class RuntimeSqlTranslator:
    """Translate the small SQLite SQL surface used by the existing workspace."""

    def __init__(self, tables: Iterable[RuntimeTable]) -> None:
        self.tables = {table.name: table for table in tables}

    def translate(self, sql: str) -> str:
        value = sql.strip()
        replace = re.match(r"INSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]+)\)", value, re.I)
        ignore = re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", value, re.I)
        if replace:
            table_name = replace.group(1)
            columns = [item.strip() for item in replace.group(2).split(",")]
            value = re.sub(r"INSERT\s+OR\s+REPLACE", "INSERT", value, count=1, flags=re.I)
            conflict_columns = self._conflict_columns(table_name, columns)
            updates = [column for column in columns if column not in conflict_columns]
            value += (
                f" ON CONFLICT({','.join(conflict_columns)}) DO UPDATE SET "
                + ",".join(f"{column}=excluded.{column}" for column in updates)
            )
        elif ignore:
            value = re.sub(r"INSERT\s+OR\s+IGNORE", "INSERT", value, count=1, flags=re.I)
            value += " ON CONFLICT DO NOTHING"
        value = re.sub(r"([A-Za-z_][A-Za-z0-9_.]*)\s+COLLATE\s+NOCASE", r"LOWER(\1)", value, flags=re.I)
        value = re.sub(
            r"datetime\(\s*'now'\s*,\s*'\+'\s*\|\|\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\|\|\s*' days'\s*\)",
            r"(CURRENT_TIMESTAMP + (\1 || ' days')::interval)", value, flags=re.I,
        )
        value = re.sub(
            r"datetime\(\s*'now'\s*,\s*'\+'\s*\|\|\s*\?\s*\|\|\s*' (minutes|hours|days)'\s*\)",
            r"(CURRENT_TIMESTAMP + (%s || ' \1')::interval)", value, flags=re.I,
        )
        value = re.sub(
            r"datetime\(\s*'now'\s*,\s*'\+(\d+) (minutes|hours|days)'\s*\)",
            r"(CURRENT_TIMESTAMP + INTERVAL '\1 \2')", value, flags=re.I,
        )
        value = re.sub(r"datetime\(\s*'now'\s*\)", "CURRENT_TIMESTAMP", value, flags=re.I)
        value = re.sub(r"datetime\(\s*\?\s*\)", "(%s)::timestamptz", value, flags=re.I)
        value = re.sub(r"datetime\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)", r"(\1)::timestamptz", value, flags=re.I)
        value = re.sub(
            r"CAST\(\s*julianday\(\s*'now'\s*\)\s*-\s*julianday\(\s*\?\s*\)\s+AS\s+INTEGER\s*\)",
            r"FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - (%s)::timestamptz))/86400)::BIGINT",
            value, flags=re.I,
        )
        # Runtime timestamp columns preserve the legacy SQLite TEXT shape.
        # PostgreSQL cannot resolve COALESCE(TEXT, TIMESTAMPTZ), so serialize
        # the fallback without changing real timestamp arithmetic elsewhere.
        value = re.sub(
            r"COALESCE\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*,\s*CURRENT_TIMESTAMP\s*\)",
            r"COALESCE(\1,to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS'))",
            value, flags=re.I,
        )
        value = value.replace("?", "%s")
        return value

    def _conflict_columns(self, table_name: str, inserted_columns: Sequence[str]) -> tuple[str, ...]:
        table = self.tables.get(table_name)
        if table is None:
            raise ValueError(f"unknown runtime table: {table_name}")
        candidates = [index.columns for index in table.indexes if index.unique and set(index.columns) <= set(inserted_columns)]
        if set(table.primary_key) <= set(inserted_columns):
            candidates.insert(0, table.primary_key)
        if not candidates:
            raise ValueError(f"INSERT OR REPLACE has no matching unique key for {table_name}")
        return candidates[0]


class RuntimeRow(MappingABC[str, Any]):
    """Mapping row that also preserves SQLite Row's positional access."""

    def __init__(self, value: Mapping[str, Any]) -> None:
        self._value = dict(value)
        self._keys = tuple(self._value)

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._value[self._keys[key]]
        return self._value[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def keys(self):
        return self._value.keys()


class PostgresRuntimeCursor:
    def __init__(self, connection: "PostgresRuntimeConnection") -> None:
        self.connection = connection
        self.cursor = connection.raw.cursor()
        self.lastrowid: int | None = None
        self._auto_returned = False

    @property
    def rowcount(self) -> int:
        return int(self.cursor.rowcount)

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> "PostgresRuntimeCursor":
        translated = self.connection.translator.translate(sql)
        self.lastrowid = None
        self._auto_returned = False
        insert = re.match(r"\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", translated, flags=re.I)
        if insert and " RETURNING " not in translated.upper():
            table = self.connection.tables.get(insert.group(1))
            if table is not None and table.primary_key == ("id",):
                translated += " RETURNING id"
                self._auto_returned = True
        self.cursor.execute(translated, tuple(parameters))
        if self._auto_returned:
            row = self.cursor.fetchone()
            if row is not None:
                self.lastrowid = int(row["id"] if isinstance(row, Mapping) else row[0])
        return self

    def fetchone(self) -> RuntimeRow | None:
        row = self.cursor.fetchone()
        return None if row is None else RuntimeRow(row)

    def fetchall(self) -> list[RuntimeRow]:
        return [RuntimeRow(row) for row in self.cursor.fetchall()]

    def __iter__(self) -> Iterator[RuntimeRow]:
        for row in self.cursor:
            yield RuntimeRow(row)

    def close(self) -> None:
        self.cursor.close()


class PostgresRuntimeConnection:
    """Small DB-API facade used by the unchanged HTTP/application workflows."""

    def __init__(
        self,
        database_url: str,
        tables: Sequence[RuntimeTable],
        *,
        schema: str = DEFAULT_RUNTIME_SCHEMA,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.database_url = database_url
        self.schema = _identifier(schema)
        self.tables = {table.name: table for table in tables}
        self.translator = RuntimeSqlTranslator(tables)
        if connection_factory is None:
            try:
                from psycopg import connect
                from psycopg.rows import dict_row
            except ImportError as error:  # pragma: no cover - production dependency
                raise RuntimeError("install requirements-postgres.txt before PostgreSQL runtime use") from error
            connection_factory = lambda url: connect(url, row_factory=dict_row)
        self.raw = connection_factory(database_url)
        with self.raw.cursor() as cursor:
            cursor.execute(f"SET search_path TO {_quoted(self.schema)}")

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> PostgresRuntimeCursor:
        return PostgresRuntimeCursor(self).execute(sql, parameters)

    def cursor(self) -> PostgresRuntimeCursor:
        return PostgresRuntimeCursor(self)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self) -> "PostgresRuntimeConnection":
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class PostgresRuntimeConnectionFactory:
    def __init__(
        self,
        database_path: str | Path,
        database_url: str,
        *,
        schema: str = DEFAULT_RUNTIME_SCHEMA,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.database_url = database_url
        self.schema = schema
        self.connection_factory = connection_factory
        self.tables = inspect_sqlite_runtime_schema(database_path)

    def connect(self) -> PostgresRuntimeConnection:
        return PostgresRuntimeConnection(
            self.database_url, self.tables, schema=self.schema,
            connection_factory=self.connection_factory,
        )


class PostgresRuntimeProjectRepository:
    """Project CRUD against the exact runtime schema, not the migration mirror."""

    editable_fields = frozenset({"name", "site_url", "industry", "default_country", "default_language"})

    def __init__(self, factory: PostgresRuntimeConnectionFactory) -> None:
        self.factory = factory

    def create_project(self, *, name: str, site_url: str, industry: str, country_code: str, language_code: str) -> dict[str, Any]:
        connection = self.factory.connect()
        try:
            with connection:
                cursor = connection.execute(
                    "INSERT INTO projects(name,site_url,industry,default_country,default_language,negative_terms_json) VALUES(?,?,?,?,?,'[]')",
                    (name, site_url, industry, country_code, language_code),
                )
                row = connection.execute(
                    "SELECT id,name,site_url,industry,default_country,default_language,created_at,updated_at FROM projects WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()
                return dict(row or {})
        finally:
            connection.close()

    def list_projects(self) -> list[dict[str, Any]]:
        connection = self.factory.connect()
        try:
            return [dict(row) for row in connection.execute(
                "SELECT id,name,site_url,industry,default_country,default_language,created_at,updated_at FROM projects ORDER BY id DESC"
            ).fetchall()]
        finally:
            connection.close()

    def list_project_summaries(self) -> list[dict[str, Any]]:
        connection = self.factory.connect()
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
        updates = [(key, value) for key, value in fields.items() if key in self.editable_fields]
        if not updates:
            raise ValueError("At least one project field is required.")
        connection = self.factory.connect()
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE projects SET " + ",".join(f"{key}=?" for key, _value in updates)
                    + ",updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (*[value for _key, value in updates], project_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("project does not exist")
                row = connection.execute(
                    "SELECT id,name,site_url,industry,default_country,default_language,created_at,updated_at FROM projects WHERE id=?",
                    (project_id,),
                ).fetchone()
                return dict(row or {})
        finally:
            connection.close()

    def delete_project(self, project_id: int) -> None:
        connection = self.factory.connect()
        try:
            with connection:
                cursor = connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
                if cursor.rowcount != 1:
                    raise ValueError("project does not exist")
        finally:
            connection.close()

    def upsert_project_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        raise RuntimeError("runtime project snapshots use the full synchronization command")


class PostgresRuntimeKeywordRepository:
    def __init__(self, factory: PostgresRuntimeConnectionFactory) -> None:
        self.factory = factory

    def list_keywords(self, project_id: int) -> list[dict[str, Any]]:
        from seo_control.infrastructure.repositories.keywords import KEYWORD_API_COLUMNS, _keyword_select
        connection = self.factory.connect()
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
        connection = self.factory.connect()
        try:
            with connection:
                if clear_all:
                    cursor = connection.execute(
                        "UPDATE keywords SET deleted_at=CURRENT_TIMESTAMP WHERE project_id=? AND deleted_at IS NULL",
                        (project_id,),
                    )
                else:
                    cursor = connection.execute(
                        "UPDATE keywords SET deleted_at=CURRENT_TIMESTAMP WHERE project_id=? AND deleted_at IS NULL AND id IN ("
                        + ",".join("?" for _value in ids) + ")",
                        (project_id, *ids),
                    )
                return cursor.rowcount
        finally:
            connection.close()


class PostgresRuntimeCollectionRepository:
    def __init__(self, factory: PostgresRuntimeConnectionFactory) -> None:
        self.factory = factory

    @staticmethod
    def _assert_project(connection: PostgresRuntimeConnection, project_id: int) -> None:
        if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise ValueError("project does not exist")

    def list_collection_plans(self, project_id: int) -> list[dict[str, Any]]:
        from seo_control.infrastructure.repositories.collection import _plan_payload
        connection = self.factory.connect()
        try:
            self._assert_project(connection, project_id)
            return [_plan_payload(row) for row in connection.execute(
                "SELECT * FROM collection_plans WHERE project_id=? ORDER BY updated_at DESC,id DESC", (project_id,)
            ).fetchall()]
        finally:
            connection.close()

    def list_catalog(self, project_id: int) -> list[dict[str, Any]]:
        from seo_control.infrastructure.repositories.collection import CATALOG_API_COLUMNS
        connection = self.factory.connect()
        try:
            self._assert_project(connection, project_id)
            return [dict(row) for row in connection.execute(
                f"SELECT {','.join(CATALOG_API_COLUMNS)} FROM competitor_url_catalog WHERE project_id=? ORDER BY last_seen_at DESC,id DESC",
                (project_id,),
            ).fetchall()]
        finally:
            connection.close()
