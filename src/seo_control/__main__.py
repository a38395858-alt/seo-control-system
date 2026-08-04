"""Command-line entry point for the SEO keyword-discovery workspace."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
from pathlib import Path

from seo_control.infrastructure.database import initialize_database
from seo_control.infrastructure.runtime_postgres import DEFAULT_RUNTIME_SCHEMA, PostgresRuntimeMigration
from seo_control.infrastructure.repositories import (
    CollectionMigrationService,
    EvidenceMigrationService,
    KeywordMigrationService,
    LegacyMigrationService,
    PostgresCollectionRepository,
    PostgresEvidenceRepository,
    PostgresKeywordRepository,
    PostgresLegacyRepository,
    PostgresProjectRepository,
    PostgresMigrationSuite,
    ProjectMigrationService,
    SQLiteCollectionRepository,
    SQLiteEvidenceRepository,
    SQLiteKeywordRepository,
    SQLiteLegacyRepository,
    SQLiteProjectRepository,
    SQLiteWorkflowRepository,
    PostgresWorkflowRepository,
    WorkflowMigrationService,
)
from seo_control.web import create_server


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "seo-control.sqlite3"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="seo_control")
    subcommands = parser.add_subparsers(dest="command", required=True)

    initialize = subcommands.add_parser("init-db", help="Create or migrate the SQLite database.")
    initialize.add_argument("--database", required=True, help="Path to the SQLite database file.")

    serve = subcommands.add_parser("serve", help="Start the local keyword-discovery web workspace.")
    serve.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    serve.add_argument("--port", default=8000, type=int, help="TCP port to bind (default: 8000).")
    serve.add_argument(
        "--database",
        default=DEFAULT_DATABASE_PATH,
        help=f"SQLite database path (default: {DEFAULT_DATABASE_PATH}).",
    )
    serve.add_argument(
        "--database-mode",
        choices=("sqlite", "shadow", "postgres"),
        default=os.getenv("SEO_RUNTIME_DATABASE_MODE"),
        help="Runtime source mode. PostgreSQL still requires the persisted cutover gate.",
    )
    serve.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="PostgreSQL target used by shadow validation.")
    serve.add_argument(
        "--runtime-state",
        default=os.getenv("SEO_RUNTIME_SWITCH_STATE_PATH"),
        help="Path to the redacted runtime cutover state JSON.",
    )

    migrate_projects = subcommands.add_parser(
        "migrate-projects-postgres",
        help="Preview or apply the first SQLite-to-PostgreSQL project migration.",
    )
    migrate_projects.add_argument("--database", default=DEFAULT_DATABASE_PATH, help="Source SQLite database path.")
    migrate_projects.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="Target PostgreSQL DATABASE_URL.")
    migrate_projects.add_argument("--apply", action="store_true", help="Write snapshots to PostgreSQL; omitted means preview only.")

    validate_projects = subcommands.add_parser(
        "validate-projects-postgres",
        help="Compare SQLite project snapshots with PostgreSQL workspace_projects.",
    )
    validate_projects.add_argument("--database", default=DEFAULT_DATABASE_PATH, help="Source SQLite database path.")
    validate_projects.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="Target PostgreSQL DATABASE_URL.")

    for module_name, label in (
        ("keywords", "keyword snapshots"),
        ("collection", "collection plans and URL catalog"),
        ("evidence", "keyword, collection, and learning evidence chains"),
        ("workflow", "titles, content workflow, approvals, publishing, and GSC feedback"),
        ("legacy", "first-party knowledge, legacy research, schedules, archives, and durable tasks"),
    ):
        migrate = subcommands.add_parser(
            f"migrate-{module_name}-postgres",
            help=f"Preview or apply SQLite-to-PostgreSQL migration for {label}.",
        )
        migrate.add_argument("--database", default=DEFAULT_DATABASE_PATH, help="Source SQLite database path.")
        migrate.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="Target PostgreSQL DATABASE_URL.")
        migrate.add_argument("--apply", action="store_true", help="Write snapshots to PostgreSQL; omitted means preview only.")
        validate = subcommands.add_parser(
            f"validate-{module_name}-postgres",
            help=f"Compare SQLite and PostgreSQL {label}.",
        )
        validate.add_argument("--database", default=DEFAULT_DATABASE_PATH, help="Source SQLite database path.")
        validate.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="Target PostgreSQL DATABASE_URL.")

    migrate_all = subcommands.add_parser(
        "migrate-all-postgres",
        help="Preview or apply all staged PostgreSQL migrations in dependency order.",
    )
    migrate_all.add_argument("--database", default=DEFAULT_DATABASE_PATH, help="Source SQLite database path.")
    migrate_all.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="Target PostgreSQL DATABASE_URL; optional for preview.")
    migrate_all.add_argument("--apply", action="store_true", help="Apply all batches; omitted means SQLite-only preview.")
    migrate_all.add_argument(
        "--confirm-full-migration",
        action="store_true",
        help="Required together with --apply to prevent accidental full-database writes.",
    )
    validate_all = subcommands.add_parser(
        "validate-all-postgres",
        help="Validate all staged PostgreSQL migration batches and emit one redacted report.",
    )
    validate_all.add_argument("--database", default=DEFAULT_DATABASE_PATH, help="Source SQLite database path.")
    validate_all.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="Target PostgreSQL DATABASE_URL.")

    migrate_runtime = subcommands.add_parser(
        "migrate-runtime-postgres",
        help="Preview or synchronize the exact live-workspace PostgreSQL schema.",
    )
    migrate_runtime.add_argument("--database", default=DEFAULT_DATABASE_PATH, help="Source SQLite database path.")
    migrate_runtime.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="Isolated PostgreSQL DATABASE_URL.")
    migrate_runtime.add_argument("--schema", default=DEFAULT_RUNTIME_SCHEMA, help="Isolated PostgreSQL runtime schema.")
    migrate_runtime.add_argument("--apply", action="store_true", help="Synchronize rows; omitted means SQLite-only preview.")
    migrate_runtime.add_argument(
        "--confirm-runtime-migration", action="store_true",
        help="Required together with --apply to prevent accidental writes.",
    )
    validate_runtime = subcommands.add_parser(
        "validate-runtime-postgres",
        help="Validate every non-secret live-workspace field in the PostgreSQL runtime schema.",
    )
    validate_runtime.add_argument("--database", default=DEFAULT_DATABASE_PATH, help="Source SQLite database path.")
    validate_runtime.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="Isolated PostgreSQL DATABASE_URL.")
    validate_runtime.add_argument("--schema", default=DEFAULT_RUNTIME_SCHEMA, help="Isolated PostgreSQL runtime schema.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "init-db":
        connection = initialize_database(arguments.database)
        connection.close()
        print(f"Database initialized: {arguments.database}")
        return 0
    if arguments.command == "serve":
        server = create_server(
            host=arguments.host, port=arguments.port, database_path=arguments.database,
            database_mode=arguments.database_mode, database_url=arguments.database_url,
            runtime_state_path=arguments.runtime_state,
        )
        host, port = server.server_address[:2]
        print(f"Keyword discovery workspace: http://{host}:{port}/")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nKeyword discovery workspace stopped.")
        finally:
            server.server_close()
        return 0
    if arguments.command in {"migrate-projects-postgres", "validate-projects-postgres"}:
        if not arguments.database_url:
            raise SystemExit("--database-url or DATABASE_URL is required")
        service = ProjectMigrationService(
            SQLiteProjectRepository(arguments.database),
            PostgresProjectRepository(arguments.database_url),
        )
        is_migration = arguments.command == "migrate-projects-postgres"
        apply = bool(getattr(arguments, "apply", False))
        report = service.migrate(apply=apply) if is_migration else service.validate()
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        if is_migration and not apply:
            return 0
        return 0 if report.valid else 2
    if arguments.command in {"migrate-all-postgres", "validate-all-postgres"}:
        apply = bool(getattr(arguments, "apply", False))
        if arguments.command == "migrate-all-postgres" and not apply:
            report = PostgresMigrationSuite(arguments.database, arguments.database_url).preview()
            print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            return 0
        if not arguments.database_url:
            raise SystemExit("--database-url or DATABASE_URL is required")
        if arguments.command == "migrate-all-postgres" and not arguments.confirm_full_migration:
            raise SystemExit("--confirm-full-migration is required together with --apply")
        suite = PostgresMigrationSuite(arguments.database, arguments.database_url)
        report = (
            suite.migrate(apply=True, confirmed=True)
            if arguments.command == "migrate-all-postgres"
            else suite.validate()
        )
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return 0 if report.valid else 2
    if arguments.command in {"migrate-runtime-postgres", "validate-runtime-postgres"}:
        apply = bool(getattr(arguments, "apply", False))
        if arguments.command == "migrate-runtime-postgres" and not apply:
            if not arguments.database_url:
                # Preview deliberately needs no live target; a placeholder is
                # never connected and is omitted from the report.
                arguments.database_url = "postgresql://preview.invalid/preview"
            report = PostgresRuntimeMigration(
                arguments.database, arguments.database_url, schema=arguments.schema
            ).preview()
            print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            return 0
        if not arguments.database_url:
            raise SystemExit("--database-url or DATABASE_URL is required")
        if arguments.command == "migrate-runtime-postgres" and not arguments.confirm_runtime_migration:
            raise SystemExit("--confirm-runtime-migration is required together with --apply")
        runtime = PostgresRuntimeMigration(
            arguments.database, arguments.database_url, schema=arguments.schema
        )
        report = runtime.apply(confirmed=True) if apply else runtime.validate()
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return 0 if report.valid else 2
    migration_modules = {
        "keywords": (KeywordMigrationService, SQLiteKeywordRepository, PostgresKeywordRepository),
        "collection": (CollectionMigrationService, SQLiteCollectionRepository, PostgresCollectionRepository),
        "evidence": (EvidenceMigrationService, SQLiteEvidenceRepository, PostgresEvidenceRepository),
        "workflow": (WorkflowMigrationService, SQLiteWorkflowRepository, PostgresWorkflowRepository),
        "legacy": (LegacyMigrationService, SQLiteLegacyRepository, PostgresLegacyRepository),
    }
    for module_name, (service_type, sqlite_type, postgres_type) in migration_modules.items():
        if arguments.command not in {f"migrate-{module_name}-postgres", f"validate-{module_name}-postgres"}:
            continue
        if not arguments.database_url:
            raise SystemExit("--database-url or DATABASE_URL is required")
        service = service_type(sqlite_type(arguments.database), postgres_type(arguments.database_url))
        is_migration = arguments.command == f"migrate-{module_name}-postgres"
        apply = bool(getattr(arguments, "apply", False))
        report = service.migrate(apply=apply) if is_migration else service.validate()
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        if is_migration and not apply:
            return 0
        return 0 if report.valid else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
