"""Contracts for P6.6 remaining-data migration and full table coverage."""

from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from typing import Any, Mapping, Sequence


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.__main__ import main  # noqa: E402
from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.infrastructure.repositories.evidence import EVIDENCE_TABLE_SPECS  # noqa: E402
from seo_control.infrastructure.repositories.legacy import (  # noqa: E402
    EXCLUDED_MIGRATION_TABLES,
    LEGACY_TABLE_SPECS,
    LegacyMigrationService,
    PostgresLegacyRepository,
    SQLiteLegacyRepository,
)
from seo_control.infrastructure.repositories.workflow import WORKFLOW_TABLE_SPECS  # noqa: E402


SPEC_BY_SOURCE = {spec.source_table: spec for spec in LEGACY_TABLE_SPECS}


def snapshot_key(row: Mapping[str, Any]) -> str:
    return f"{row['source_table']}:{int(row['id'])}"


class BatchMemoryTarget:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.batch_count = 0
        self.single_count = 0
        self.reset_count = 0

    def list_migration_snapshots(self) -> list[dict[str, Any]]:
        return [deepcopy(row) for _key, row in sorted(self.rows.items())]

    def upsert_migration_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        self.single_count += 1
        self.rows[snapshot_key(snapshot)] = deepcopy(dict(snapshot))

    def upsert_migration_snapshots(self, snapshots: Sequence[Mapping[str, Any]]) -> None:
        self.batch_count += 1
        for snapshot in snapshots:
            self.rows[snapshot_key(snapshot)] = deepcopy(dict(snapshot))

    def reset_identity(self) -> None:
        self.reset_count += 1


class LegacyRepositoryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "p66.sqlite3"
        connection = initialize_database(self.database_path)
        with connection:
            self.first_project = int(connection.execute("INSERT INTO projects(name) VALUES('first')").lastrowid)
            self.second_project = int(connection.execute("INSERT INTO projects(name) VALUES('second')").lastrowid)
        connection.close()
        self.repository = SQLiteLegacyRepository(self.database_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seed_legacy(self, project_id: int, suffix: str) -> dict[str, int]:
        connection = initialize_database(self.database_path)
        try:
            with connection:
                keyword_id = int(connection.execute(
                    """INSERT INTO keywords(project_id,keyword,normalized_keyword,country_code,language_code)
                       VALUES(?,?,?,?,?)""",
                    (project_id, f"Keyword {suffix}", f"keyword {suffix}", "US", "en"),
                ).lastrowid)
                candidate_id = int(connection.execute(
                    """INSERT INTO keyword_title_candidates(
                           project_id,keyword_id,title,normalized_title,source_type,status
                       ) VALUES(?,?,?,?,?,'selected')""",
                    (project_id, keyword_id, f"Title {suffix}", f"title {suffix}", "manual"),
                ).lastrowid)
                asset_id = int(connection.execute(
                    """INSERT INTO content_assets(
                           project_id,keyword_id,selected_title_candidate_id,title_snapshot
                       ) VALUES(?,?,?,?)""",
                    (project_id, keyword_id, candidate_id, f"Title {suffix}"),
                ).lastrowid)
                knowledge_id = int(connection.execute(
                    """INSERT INTO project_knowledge_documents(
                           project_id,title,source_type,url,content,knowledge_type,status,
                           summary,tags_json,classification_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (project_id, "Brand facts", "manual", f"https://{suffix}.test/about", "Verified", "brand", "ready", "Facts", '["brand"]', '{"verified":true}'),
                ).lastrowid)
                crawl_run_id = int(connection.execute(
                    """INSERT INTO project_knowledge_crawl_runs(
                           project_id,status,max_pages,discovered_count,accepted_count
                       ) VALUES(?,'completed',?,?,?)""",
                    (project_id, 20, 1, 1),
                ).lastrowid)
                crawl_page_id = int(connection.execute(
                    """INSERT INTO project_knowledge_crawl_pages(
                           crawl_run_id,url,title,knowledge_type,status
                       ) VALUES(?,?,?,?,?)""",
                    (crawl_run_id, f"https://{suffix}.test/about", "About", "brand", "accepted"),
                ).lastrowid)
                connection.execute(
                    """INSERT INTO authority_search_results(
                           search_run_id,project_id,content_asset_id,section_heading,claim_topic,
                           search_query,rank,title,url,domain,status
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"search-{suffix}", project_id, asset_id, "Facts", "standard", "official standard", 1, "Official", f"https://gov.{suffix}/standard", f"gov.{suffix}", "accepted"),
                )
                connection.execute(
                    """INSERT INTO authority_url_exclusions(
                           project_id,normalized_url,reason
                       ) VALUES(?,?,?)""",
                    (project_id, f"https://spam.{suffix}/", "low_authority"),
                )
                schedule_id = int(connection.execute(
                    """INSERT INTO competitor_learning_schedules(
                           project_id,topics_json,interval_days,enabled,provider,model
                       ) VALUES(?,?,?,?,?,?)""",
                    (project_id, '["structure","style"]', 14, 1, "deepseek", "test-model"),
                ).lastrowid)
                learning_run_id = int(connection.execute(
                    """INSERT INTO competitor_learning_runs(
                           project_id,schedule_id,content_asset_id,topic,trigger_type,status,
                           source_count,cards_created
                       ) VALUES(?,?,?,?,?,'completed',?,?)""",
                    (project_id, schedule_id, asset_id, "structure", "scheduled", 3, 1),
                ).lastrowid)
                connection.execute(
                    """INSERT INTO competitor_style_cards(
                           project_id,schedule_id,learning_run_id,topic,card_title,summary,
                           evidence_json,source_signature,quality_score
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (project_id, schedule_id, learning_run_id, "structure", "Short sections", "Use concise H2 sections", '{"sources":[1,2,3]}', f"sig-{suffix}", 0.85),
                )
                research_run_id = int(connection.execute(
                    """INSERT INTO competitor_research_runs(
                           project_id,content_asset_id,query,locale,provider,model,status,
                           discovered_count,usable_count,analysis_json
                       ) VALUES(?,?,?,?,?,?,'completed',?,?,?)""",
                    (project_id, asset_id, f"query {suffix}", "en-US", "serper", "test-model", 1, 1, '{"gap":"examples"}'),
                ).lastrowid)
                research_item_id = int(connection.execute(
                    """INSERT INTO competitor_research_items(
                           research_run_id,rank,search_title,url,domain,status
                       ) VALUES(?,?,?,?,?,'selected')""",
                    (research_run_id, 1, "Competitor", f"https://competitor.{suffix}/guide", f"competitor.{suffix}"),
                ).lastrowid)
                connection.execute(
                    """INSERT INTO competitor_url_archive(
                           project_id,normalized_url,url,domain,status,last_rank,last_query
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (project_id, f"https://blocked.{suffix}/", f"https://blocked.{suffix}/", f"blocked.{suffix}", "robots_blocked", 2, "query"),
                )
                task_id = int(connection.execute(
                    """INSERT INTO durable_task_queue(
                           project_id,task_type,resource_id,payload_json,dedup_key,status,
                           attempt_count,max_attempts,available_at,lease_expires_at,celery_task_id,last_error
                       ) VALUES(?,?,?,?,?,'retry_wait',?,?,?,?,?,?)""",
                    (project_id, "competitor_learning", learning_run_id, '{"topic":"structure"}', f"task-{suffix}", 1, 3, "2026-07-31 10:00:00", "2026-07-31 10:05:00", f"celery-{suffix}", "temporary"),
                ).lastrowid)
            return {
                "knowledge": knowledge_id, "crawl_run": crawl_run_id, "crawl_page": crawl_page_id,
                "research_run": research_run_id, "research_item": research_item_id, "task": task_id,
            }
        finally:
            connection.close()

    def test_all_sqlite_business_tables_have_a_migration_owner_or_explicit_exclusion(self) -> None:
        connection = initialize_database(self.database_path)
        try:
            sqlite_tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            connection.close()
        covered = {
            "projects", "keywords", "collection_plans", "competitor_url_catalog",
            *(spec.source_table for spec in EVIDENCE_TABLE_SPECS),
            *(spec.source_table for spec in WORKFLOW_TABLE_SPECS),
            *(spec.source_table for spec in LEGACY_TABLE_SPECS),
        }
        self.assertEqual(sqlite_tables, covered | set(EXCLUDED_MIGRATION_TABLES))
        self.assertEqual({"schema_migrations", "gsc_oauth_connection"}, set(EXCLUDED_MIGRATION_TABLES))

    def test_twelve_specs_infer_projects_and_preserve_recoverable_task_state(self) -> None:
        first = self.seed_legacy(self.first_project, "first")
        second = self.seed_legacy(self.second_project, "second")
        self.assertEqual(12, len(LEGACY_TABLE_SPECS))
        rows = self.repository.list_migration_snapshots()
        by_key = {snapshot_key(row): row for row in rows}
        self.assertEqual(self.first_project, by_key[f"project_knowledge_crawl_pages:{first['crawl_page']}"]["project_id"])
        self.assertEqual(self.second_project, by_key[f"competitor_research_items:{second['research_item']}"]["project_id"])
        task = by_key[f"durable_task_queue:{first['task']}"]
        self.assertEqual("retry_wait", task["status"])
        self.assertEqual(1, task["attempt_count"])
        self.assertEqual("celery-first", task["celery_task_id"])

    def test_batch_migration_is_idempotent_and_normalizes_json(self) -> None:
        self.seed_legacy(self.first_project, "first")
        target = BatchMemoryTarget()
        service = LegacyMigrationService(self.repository, target)  # type: ignore[arg-type]
        self.assertFalse(service.migrate(apply=False).applied)
        first = service.migrate(apply=True)
        second = service.migrate(apply=True)
        self.assertTrue(first.valid)
        self.assertTrue(second.valid)
        self.assertEqual(2, target.batch_count)
        self.assertEqual(0, target.single_count)
        self.assertEqual(2, target.reset_count)
        document = target.rows[next(key for key in target.rows if key.startswith("project_knowledge_documents:"))]
        document["classification_json"] = {"verified": True}
        self.assertTrue(service.validate().valid)

    def test_validation_reports_table_qualified_mismatch_and_missing(self) -> None:
        self.seed_legacy(self.first_project, "first")
        target = BatchMemoryTarget()
        service = LegacyMigrationService(self.repository, target)  # type: ignore[arg-type]
        service.migrate(apply=True)
        task_key = next(key for key in target.rows if key.startswith("durable_task_queue:"))
        target.rows[task_key]["last_error"] = "wrong"
        mismatch = service.validate()
        self.assertEqual(task_key, mismatch.mismatches[0]["key"])
        self.assertIn("last_error", mismatch.mismatches[0]["fields"])
        target.rows.pop(task_key)
        self.assertEqual((task_key,), service.validate().missing_keys)

    def test_dry_run_and_cli_preview_do_not_connect_to_postgres(self) -> None:
        self.seed_legacy(self.first_project, "first")

        def unavailable(_database_url: str):
            raise ConnectionError("postgres unavailable")

        target = PostgresLegacyRepository("postgresql://redacted", connection_factory=unavailable)
        self.assertFalse(LegacyMigrationService(self.repository, target).migrate(apply=False).applied)
        with redirect_stdout(StringIO()):
            result = main([
                "migrate-legacy-postgres", "--database", str(self.database_path),
                "--database-url", "postgresql://preview-only",
            ])
        self.assertEqual(0, result)
        self.assertGreater(len(self.repository.list_migration_snapshots()), 0)


if __name__ == "__main__":
    unittest.main()
