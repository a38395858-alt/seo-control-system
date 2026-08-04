"""Contracts for P6.4 historical and evidence-chain migration."""

from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any, Mapping, Sequence


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.__main__ import main  # noqa: E402
from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.infrastructure.repositories.evidence import (  # noqa: E402
    EVIDENCE_TABLE_SPECS,
    EvidenceMigrationService,
    PostgresEvidenceRepository,
    SQLiteEvidenceRepository,
)


SPEC_BY_SOURCE = {spec.source_table: spec for spec in EVIDENCE_TABLE_SPECS}


def snapshot_key(row: Mapping[str, Any]) -> str:
    spec = SPEC_BY_SOURCE[str(row["source_table"])]
    return f"{spec.source_table}:" + ":".join(str(row[column]) for column in spec.key_columns)


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


class EvidenceRepositoryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "p64.sqlite3"
        connection = initialize_database(self.database_path)
        with connection:
            self.first_project = int(connection.execute("INSERT INTO projects(name) VALUES('first')").lastrowid)
            self.second_project = int(connection.execute("INSERT INTO projects(name) VALUES('second')").lastrowid)
        connection.close()
        self.repository = SQLiteEvidenceRepository(self.database_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seed_keyword_chain(self, project_id: int, suffix: str) -> dict[str, int]:
        connection = initialize_database(self.database_path)
        try:
            with connection:
                task_id = int(connection.execute(
                    """INSERT INTO keyword_research_tasks(
                           project_id,source_type,country_code,language_code,parameters_json,status
                       ) VALUES(?,?,?,?,?,?)""",
                    (project_id, "google_suggest", "US", "en", '{"depth":2,"letters":["a","b"]}', "completed"),
                ).lastrowid)
                batch_id = int(connection.execute(
                    """INSERT INTO import_batches(
                           project_id,task_id,original_filename,file_sha256,mapping_json,status
                       ) VALUES(?,?,?,?,?,?)""",
                    (project_id, task_id, f"{suffix}.csv", suffix * 8, '{"keyword":"term"}', "completed"),
                ).lastrowid)
                import_row_id = int(connection.execute(
                    "INSERT INTO import_rows(import_batch_id,row_number,keyword,status) VALUES(?,?,?,'accepted')",
                    (batch_id, 1, f"keyword {suffix}"),
                ).lastrowid)
                suggest_id = int(connection.execute(
                    """INSERT INTO suggest_query_jobs(task_id,query,normalized_query,hl,gl,status)
                       VALUES(?,?,?,?,?,'completed')""",
                    (task_id, f"query {suffix}", f"query {suffix}", "en", "us"),
                ).lastrowid)
                keyword_id = int(connection.execute(
                    """INSERT INTO keywords(project_id,keyword,normalized_keyword,country_code,language_code)
                       VALUES(?,?,?,?,?)""",
                    (project_id, f"Keyword {suffix}", f"keyword {suffix}", "US", "en"),
                ).lastrowid)
                category_id = int(connection.execute(
                    "INSERT INTO keyword_categories(project_id,name,normalized_name) VALUES(?,?,?)",
                    (project_id, f"Category {suffix}", f"category {suffix}"),
                ).lastrowid)
                connection.execute(
                    """INSERT INTO keyword_category_assignments(keyword_id,category_id,source,confidence)
                       VALUES(?,?,?,?)""",
                    (keyword_id, category_id, "ai", 0.81),
                )
                review_id = int(connection.execute(
                    """INSERT INTO keyword_reviews(
                           keyword_id,seed_keyword,provider,is_seo_content_fit,same_topic_as_seed,confidence
                       ) VALUES(?,?,?,1,1,?)""",
                    (keyword_id, f"seed {suffix}", "rule", 0.9),
                ).lastrowid)
                source_id = int(connection.execute(
                    """INSERT INTO keyword_sources(
                           keyword_id,source_type,task_id,import_batch_id,seed_keyword
                       ) VALUES(?,?,?,?,?)""",
                    (keyword_id, "file_import", task_id, batch_id, f"seed {suffix}"),
                ).lastrowid)
                metric_id = int(connection.execute(
                    """INSERT INTO keyword_metric_snapshots(
                           keyword_id,source_type,metric_date,country_code,language_code,
                           monthly_search_volumes_json,average_monthly_searches
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (keyword_id, "file_import", "2026-07-01", "US", "en", '[{"month":7,"value":100}]', 100),
                ).lastrowid)
            return {
                "task": task_id, "batch": batch_id, "row": import_row_id, "suggest": suggest_id,
                "keyword": keyword_id, "category": category_id, "review": review_id,
                "source": source_id, "metric": metric_id,
            }
        finally:
            connection.close()

    def seed_collection_chain(self, project_id: int, suffix: str) -> None:
        connection = initialize_database(self.database_path)
        try:
            with connection:
                catalog_id = int(connection.execute(
                    """INSERT INTO competitor_url_catalog(
                           project_id,normalized_url,url,domain,collection_status
                       ) VALUES(?,?,?,?,?)""",
                    (project_id, f"https://{suffix}.test/page", f"https://{suffix}.test/page", f"{suffix}.test", "collected"),
                ).lastrowid)
                run_id = int(connection.execute(
                    """INSERT INTO competitor_catalog_collection_runs(
                           project_id,status,candidate_ids_json,total_count,collected_count
                       ) VALUES(?,?,?,?,?)""",
                    (project_id, "completed", json.dumps([catalog_id]), 1, 1),
                ).lastrowid)
                memory_id = int(connection.execute(
                    """INSERT INTO competitor_content_memory(
                           project_id,normalized_url,url,domain,page_title,content,content_hash,structure_json
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (project_id, f"https://{suffix}.test/page", f"https://{suffix}.test/page", f"{suffix}.test", "Page", "Body", f"hash-{suffix}", '{"h2":["Intro"]}'),
                ).lastrowid)
                connection.execute(
                    "INSERT INTO competitor_content_chunks(project_id,memory_id,position,content) VALUES(?,?,0,'Body')",
                    (project_id, memory_id),
                )
                version_id = int(connection.execute(
                    """INSERT INTO competitor_content_versions(
                           project_id,memory_id,content_hash,content,structure_json,extractor
                       ) VALUES(?,?,?,?,?,?)""",
                    (project_id, memory_id, f"hash-{suffix}", "Body", '{"h2":["Intro"]}', "test"),
                ).lastrowid)
                connection.execute(
                    """INSERT INTO collection_run_items(
                           project_id,run_id,catalog_id,normalized_url,source_url,status,memory_id,version_id
                       ) VALUES(?,?,?,?,?,'collected',?,?)""",
                    (project_id, run_id, catalog_id, f"https://{suffix}.test/page", f"https://{suffix}.test/page", memory_id, version_id),
                )
                connection.execute(
                    """INSERT INTO competitor_content_learning_runs(
                           project_id,status,candidate_memory_ids_json,source_count,processed_count
                       ) VALUES(?,?,?,?,?)""",
                    (project_id, "completed", json.dumps([memory_id]), 1, 1),
                )
                learning_id = int(connection.execute(
                    """INSERT INTO content_learning_memories(
                           project_id,memory_type,topic,summary,evidence_json,source_url,
                           source_content_hash,quality_score,applicability_json
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (project_id, "style", "structure", "Use short sections", '{"quote":"Body","rank":1}', f"https://{suffix}.test/page", f"hash-{suffix}", 0.88, '{"language":"en","intent":"guide"}'),
                ).lastrowid)
                connection.execute(
                    """INSERT INTO content_learning_memory_sources(
                           project_id,memory_id,source_type,source_id,source_url,source_content_hash,
                           source_version_id,evidence_excerpt
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (project_id, learning_id, "competitor_content", str(memory_id), f"https://{suffix}.test/page", f"hash-{suffix}", version_id, "Body"),
                )
                connection.execute(
                    """INSERT INTO content_learning_memory_feedback(project_id,memory_id,decision,note)
                       VALUES(?,?,?,?)""",
                    (project_id, learning_id, "useful", "verified"),
                )
        finally:
            connection.close()

    def test_specs_cover_all_eighteen_tables_in_dependency_order(self) -> None:
        self.assertEqual(18, len(EVIDENCE_TABLE_SPECS))
        self.assertEqual("keyword_research_tasks", EVIDENCE_TABLE_SPECS[0].source_table)
        self.assertEqual("content_learning_memory_feedback", EVIDENCE_TABLE_SPECS[-1].source_table)
        self.assertTrue(all("project_id" in spec.columns for spec in EVIDENCE_TABLE_SPECS))
        assignment = SPEC_BY_SOURCE["keyword_category_assignments"]
        self.assertEqual(("keyword_id", "category_id"), assignment.key_columns)
        memory_ddl = PostgresEvidenceRepository._ddl(SPEC_BY_SOURCE["content_learning_memories"])
        self.assertNotIn("FOREIGN KEY(superseded_by_id)", memory_ddl)

    def test_sqlite_snapshots_infer_project_and_keep_projects_isolated(self) -> None:
        first = self.seed_keyword_chain(self.first_project, "first")
        second = self.seed_keyword_chain(self.second_project, "second")
        self.seed_collection_chain(self.first_project, "first")
        rows = self.repository.list_migration_snapshots()
        self.assertEqual(27, len(rows))

        by_key = {snapshot_key(row): row for row in rows}
        self.assertEqual(self.first_project, by_key[f"import_rows:{first['row']}"]["project_id"])
        self.assertEqual(self.second_project, by_key[f"suggest_query_jobs:{second['suggest']}"]["project_id"])
        self.assertEqual(self.first_project, by_key[f"keyword_reviews:{first['review']}"]["project_id"])
        self.assertEqual(self.second_project, by_key[f"keyword_sources:{second['source']}"]["project_id"])
        assignment_key = f"keyword_category_assignments:{first['keyword']}:{first['category']}"
        self.assertEqual(self.first_project, by_key[assignment_key]["project_id"])

    def test_migration_uses_batch_upsert_is_idempotent_and_normalizes_json_time(self) -> None:
        self.seed_keyword_chain(self.first_project, "first")
        self.seed_collection_chain(self.first_project, "first")
        target = BatchMemoryTarget()
        service = EvidenceMigrationService(self.repository, target)  # type: ignore[arg-type]

        preview = service.migrate(apply=False)
        self.assertFalse(preview.applied)
        self.assertEqual(0, target.batch_count)
        first = service.migrate(apply=True)
        second = service.migrate(apply=True)
        self.assertTrue(first.valid)
        self.assertTrue(second.valid)
        self.assertEqual(2, target.batch_count)
        self.assertEqual(0, target.single_count)
        self.assertEqual(2, target.reset_count)

        row = target.rows[next(key for key in target.rows if key.startswith("content_learning_memories:"))]
        row["evidence_json"] = {"rank": 1, "quote": "Body"}
        row["created_at"] = datetime.fromisoformat(str(row["created_at"])).replace(tzinfo=timezone.utc)
        self.assertTrue(service.validate().valid)

    def test_validation_reports_table_qualified_missing_extra_and_mismatch(self) -> None:
        self.seed_keyword_chain(self.first_project, "first")
        target = BatchMemoryTarget()
        service = EvidenceMigrationService(self.repository, target)  # type: ignore[arg-type]
        service.migrate(apply=True)

        review_key = next(key for key in target.rows if key.startswith("keyword_reviews:"))
        target.rows[review_key]["reason"] = "wrong"
        mismatch = service.validate()
        self.assertEqual(review_key, mismatch.mismatches[0]["key"])
        self.assertIn("reason", mismatch.mismatches[0]["fields"])

        removed = target.rows.pop(review_key)
        missing = service.validate()
        self.assertEqual((review_key,), missing.missing_keys)
        removed["id"] += 1000
        target.rows[snapshot_key(removed)] = removed
        extra = service.validate()
        self.assertTrue(any(key.startswith("keyword_reviews:") for key in extra.extra_keys))

    def test_dry_run_and_cli_preview_do_not_connect_to_postgres(self) -> None:
        self.seed_keyword_chain(self.first_project, "first")

        def unavailable(_database_url: str):
            raise ConnectionError("postgres unavailable")

        target = PostgresEvidenceRepository("postgresql://redacted", connection_factory=unavailable)
        report = EvidenceMigrationService(self.repository, target).migrate(apply=False)
        self.assertEqual(9, report.source_count)
        self.assertFalse(report.applied)
        with redirect_stdout(StringIO()):
            result = main([
                "migrate-evidence-postgres", "--database", str(self.database_path),
                "--database-url", "postgresql://preview-only",
            ])
        self.assertEqual(0, result)
        self.assertEqual(9, len(self.repository.list_migration_snapshots()))


if __name__ == "__main__":
    unittest.main()
