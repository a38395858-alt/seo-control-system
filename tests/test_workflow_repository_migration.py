"""Contracts for P6.5 content workflow and integration-data migration."""

from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
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
from seo_control.infrastructure.repositories.workflow import (  # noqa: E402
    PostgresWorkflowRepository,
    SQLiteWorkflowRepository,
    WORKFLOW_TABLE_SPECS,
    WorkflowMigrationService,
)


SPEC_BY_SOURCE = {spec.source_table: spec for spec in WORKFLOW_TABLE_SPECS}


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


class WorkflowRepositoryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "p65.sqlite3"
        connection = initialize_database(self.database_path)
        with connection:
            self.first_project = int(connection.execute("INSERT INTO projects(name) VALUES('first')").lastrowid)
            self.second_project = int(connection.execute("INSERT INTO projects(name) VALUES('second')").lastrowid)
        connection.close()
        self.repository = SQLiteWorkflowRepository(self.database_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seed_workflow(self, project_id: int, suffix: str) -> dict[str, int]:
        connection = initialize_database(self.database_path)
        try:
            with connection:
                keyword_id = int(connection.execute(
                    """INSERT INTO keywords(project_id,keyword,normalized_keyword,country_code,language_code)
                       VALUES(?,?,?,?,?)""",
                    (project_id, f"Keyword {suffix}", f"keyword {suffix}", "US", "en"),
                ).lastrowid)
                connection.execute(
                    """INSERT INTO serp_title_samples(
                           project_id,keyword_id,rank,title,normalized_title,source_type
                       ) VALUES(?,?,?,?,?,'browser')""",
                    (project_id, keyword_id, 1, f"SERP {suffix}", f"serp {suffix}"),
                )
                title_job_id = int(connection.execute(
                    """INSERT INTO title_generation_jobs(
                           project_id,keyword_id,status,request_json,provider,idempotency_key
                       ) VALUES(?,?,'succeeded',?,'rule',?)""",
                    (project_id, keyword_id, '{"count":8,"locale":"en-US"}', f"title-{suffix}"),
                ).lastrowid)
                candidate_id = int(connection.execute(
                    """INSERT INTO keyword_title_candidates(
                           project_id,keyword_id,generation_job_id,title,normalized_title,source_type,
                           quality_details_json,status
                       ) VALUES(?,?,?,?,?,'ai',?,'selected')""",
                    (project_id, keyword_id, title_job_id, f"Title {suffix}", f"title {suffix}", '{"clarity":90,"intent":"guide"}'),
                ).lastrowid)
                connection.execute(
                    """INSERT INTO keyword_title_selection_events(
                           project_id,keyword_id,selected_candidate_id,action
                       ) VALUES(?,?,?,'selected')""",
                    (project_id, keyword_id, candidate_id),
                )
                asset_id = int(connection.execute(
                    """INSERT INTO content_assets(
                           project_id,keyword_id,selected_title_candidate_id,title_snapshot,tags_json
                       ) VALUES(?,?,?,?,?)""",
                    (project_id, keyword_id, candidate_id, f"Title {suffix}", '["guide","seo"]'),
                ).lastrowid)
                authority_id = int(connection.execute(
                    """INSERT INTO authority_source_library(
                           project_id,title,source_type,url,content,tags_json,classification_json
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (project_id, "Official source", "government", f"https://{suffix}.test/source", "Verified fact", '["fact"]', '{"risk":"low"}'),
                ).lastrowid)
                agent_job_id = int(connection.execute(
                    """INSERT INTO agent_jobs(
                           project_id,content_asset_id,requested_action,status,input_json,result_json,checkpoint_json
                       ) VALUES(?,?,?,'completed',?,?,?)""",
                    (project_id, asset_id, "generate", '{"mode":"memory"}', '{"draft":1}', '{"node":"done"}'),
                ).lastrowid)
                brief_id = int(connection.execute(
                    """INSERT INTO content_briefs(
                           content_asset_id,target_audience,business_goal,target_length,sources_json,
                           brief_json,agent_job_id
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (asset_id, "buyers", "educate", 1200, '["source-1"]', '{"intent":"guide"}', agent_job_id),
                ).lastrowid)
                outline_id = int(connection.execute(
                    """INSERT INTO content_outlines(content_asset_id,brief_id,status,agent_job_id)
                       VALUES(?,?,'locked',?)""",
                    (asset_id, brief_id, agent_job_id),
                ).lastrowid)
                section_id = int(connection.execute(
                    """INSERT INTO content_outline_sections(
                           outline_id,position,heading,purpose,word_budget,section_json
                       ) VALUES(?,?,?,?,?,?)""",
                    (outline_id, 1, "Introduction", "Explain", 200, '{"facts":["source-1"]}'),
                ).lastrowid)
                generation_job_id = int(connection.execute(
                    """INSERT INTO content_generation_jobs(
                           project_id,content_asset_id,requested_action,provider,model,status
                       ) VALUES(?,?,?,?,?,'completed')""",
                    (project_id, asset_id, "generate", "openai", "test-model"),
                ).lastrowid)
                generation_run_id = int(connection.execute(
                    """INSERT INTO content_generation_runs(
                           project_id,content_asset_id,stage,provider,model,status,input_json,output_json,
                           generation_job_id
                       ) VALUES(?,?,'assembly','openai',?,'completed',?,?,?)""",
                    (project_id, asset_id, "test-model", '{"outline":1}', '{"markdown":"Body"}', generation_job_id),
                ).lastrowid)
                draft_id = int(connection.execute(
                    """INSERT INTO content_drafts(
                           project_id,content_asset_id,outline_id,generation_run_id,version,title,markdown,
                           sources_used_json,unresolved_verify_json,qa_json,provider,model,generation_job_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (project_id, asset_id, outline_id, generation_run_id, 1, f"Title {suffix}", "# Body", '["source-1"]', '[]', '{"score":90}', "openai", "test-model", generation_job_id),
                ).lastrowid)
                connection.execute(
                    """UPDATE content_assets SET current_brief_id=?,current_outline_id=?,current_draft_id=?,
                           current_generation_run_id=? WHERE id=?""",
                    (brief_id, outline_id, draft_id, generation_run_id, asset_id),
                )
                connection.execute(
                    """INSERT INTO agent_steps(
                           job_id,node_name,status,output_json,input_json,token_usage_json,duration_ms,cost_micros
                       ) VALUES(?,'write','completed',?,?,?,?,?)""",
                    (agent_job_id, '{"draft":1}', '{"outline":1}', '{"total":100}', 125, 2500),
                )
                connection.execute(
                    """INSERT INTO agent_approval_requests(
                           project_id,job_id,approval_type,payload_json,status,decided_by
                       ) VALUES(?,?,'blueprint',?,'approved','tester')""",
                    (project_id, agent_job_id, '{"outline":1}'),
                )
                connection.execute(
                    """INSERT INTO agent_tool_audits(
                           project_id,job_id,tool_name,status,input_json,output_json,duration_ms
                       ) VALUES(?,?,'generate_article','completed',?,?,?)""",
                    (project_id, agent_job_id, '{"asset":1}', '{"ok":true}', 80),
                )
                memory_id = int(connection.execute(
                    """INSERT INTO content_learning_memories(
                           project_id,memory_type,topic,summary,evidence_json,quality_score
                       ) VALUES(?,'style','structure','Short sections',?,?)""",
                    (project_id, '{"source":"test"}', 0.8),
                ).lastrowid)
                connection.execute(
                    """INSERT INTO content_memory_links(
                           content_asset_id,memory_id,role,relevance_score,selected_by_model
                       ) VALUES(?,?,'style',?,1)""",
                    (asset_id, memory_id, 0.9),
                )
                connection.execute(
                    """INSERT INTO content_authority_source_links(
                           project_id,content_asset_id,authority_source_id,section_heading,claim_topic
                       ) VALUES(?,?,?,?,?)""",
                    (project_id, asset_id, authority_id, "Introduction", "fact"),
                )
                connection.execute(
                    """INSERT INTO content_section_images(
                           project_id,content_asset_id,draft_id,section_heading,position,prompt,alt_text,status
                       ) VALUES(?,?,?,?,?,?,?,'ready')""",
                    (project_id, asset_id, draft_id, "Introduction", 1, "Product image", "Product"),
                )
                connection.execute(
                    """INSERT INTO content_generation_basis_reports(
                           project_id,agent_job_id,content_asset_id,draft_id,report_json
                       ) VALUES(?,?,?,?,?)""",
                    (project_id, agent_job_id, asset_id, draft_id, '{"memories":[1],"sources":[1]}'),
                )
                connection.execute(
                    """INSERT INTO project_wordpress_configs(
                           project_id,site_url,username,application_password
                       ) VALUES(?,?,?,?)""",
                    (project_id, f"https://{suffix}.test", "publisher", "super-secret"),
                )
                connection.execute(
                    """INSERT INTO content_publish_gate_reports(
                           project_id,content_asset_id,draft_id,requested_status,report_json,status
                       ) VALUES(?,?,?,'draft',?,'ready')""",
                    (project_id, asset_id, draft_id, '{"facts":true,"qa":90}'),
                )
                connection.execute(
                    """INSERT INTO content_wordpress_publications(
                           project_id,content_asset_id,draft_id,wordpress_post_id,wordpress_url,status
                       ) VALUES(?,?,?,?,?,'draft')""",
                    (project_id, asset_id, draft_id, 99, f"https://{suffix}.test/post"),
                )
                connection.execute(
                    "INSERT INTO project_gsc_properties(project_id,property_url) VALUES(?,?)",
                    (project_id, f"sc-domain:{suffix}.test"),
                )
                connection.execute(
                    """INSERT INTO project_gsc_query_rows(
                           project_id,property_url,query,page_url,clicks,impressions,ctr,position
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (project_id, f"sc-domain:{suffix}.test", f"query {suffix}", f"https://{suffix}.test/post", 5.0, 100.0, 0.05, 4.2),
                )
                gsc_snapshot_id = int(connection.execute(
                    """INSERT INTO content_gsc_performance_snapshots(
                           project_id,content_asset_id,draft_id,published_url,window_days,clicks,
                           impressions,ctr,average_position,query_count,learning_status
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (project_id, asset_id, draft_id, f"https://{suffix}.test/post", 7, 5.0, 100.0, 0.05, 4.2, 1, "observing"),
                ).lastrowid)
                gsc_row_id = int(connection.execute(
                    """INSERT INTO content_gsc_performance_rows(
                           snapshot_id,query,page_url,clicks,impressions,ctr,position
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (gsc_snapshot_id, f"query {suffix}", f"https://{suffix}.test/post", 5.0, 100.0, 0.05, 4.2),
                ).lastrowid)
            return {
                "asset": asset_id, "brief": brief_id, "outline": outline_id, "section": section_id,
                "agent_job": agent_job_id, "gsc_snapshot": gsc_snapshot_id, "gsc_row": gsc_row_id,
            }
        finally:
            connection.close()

    def test_specs_cover_twenty_seven_tables_and_exclude_credentials(self) -> None:
        self.assertEqual(27, len(WORKFLOW_TABLE_SPECS))
        self.assertEqual("serp_title_samples", WORKFLOW_TABLE_SPECS[0].source_table)
        self.assertEqual("content_gsc_performance_rows", WORKFLOW_TABLE_SPECS[-1].source_table)
        self.assertTrue(all("project_id" in spec.columns for spec in WORKFLOW_TABLE_SPECS))
        wordpress = SPEC_BY_SOURCE["project_wordpress_configs"]
        self.assertNotIn("application_password", wordpress.columns)
        self.assertIn("requires_reauthorization", wordpress.columns)
        self.assertNotIn("gsc_oauth_connection", SPEC_BY_SOURCE)

    def test_source_snapshots_infer_project_and_sanitize_wordpress_secret(self) -> None:
        first = self.seed_workflow(self.first_project, "first")
        second = self.seed_workflow(self.second_project, "second")
        rows = self.repository.list_migration_snapshots()
        by_key = {snapshot_key(row): row for row in rows}
        self.assertEqual(self.first_project, by_key[f"content_briefs:{first['brief']}"]["project_id"])
        self.assertEqual(self.second_project, by_key[f"content_outline_sections:{second['section']}"]["project_id"])
        self.assertEqual(self.first_project, by_key[f"agent_steps:1"]["project_id"])
        self.assertEqual(self.second_project, by_key[f"content_gsc_performance_rows:{second['gsc_row']}"]["project_id"])
        wordpress = by_key[f"project_wordpress_configs:{self.first_project}"]
        self.assertEqual(1, wordpress["requires_reauthorization"])
        self.assertNotIn("application_password", wordpress)
        self.assertNotIn("super-secret", repr(rows))

    def test_migration_is_batch_idempotent_and_json_order_is_equivalent(self) -> None:
        self.seed_workflow(self.first_project, "first")
        target = BatchMemoryTarget()
        service = WorkflowMigrationService(self.repository, target)  # type: ignore[arg-type]
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

        candidate = target.rows[next(key for key in target.rows if key.startswith("keyword_title_candidates:"))]
        candidate["quality_details_json"] = {"intent": "guide", "clarity": 90}
        self.assertTrue(service.validate().valid)

    def test_validation_reports_table_qualified_differences(self) -> None:
        self.seed_workflow(self.first_project, "first")
        target = BatchMemoryTarget()
        service = WorkflowMigrationService(self.repository, target)  # type: ignore[arg-type]
        service.migrate(apply=True)
        draft_key = next(key for key in target.rows if key.startswith("content_drafts:"))
        target.rows[draft_key]["markdown"] = "wrong"
        mismatch = service.validate()
        self.assertEqual(draft_key, mismatch.mismatches[0]["key"])
        self.assertIn("markdown", mismatch.mismatches[0]["fields"])
        target.rows.pop(draft_key)
        self.assertEqual((draft_key,), service.validate().missing_keys)

    def test_dry_run_and_cli_preview_do_not_connect_to_postgres(self) -> None:
        self.seed_workflow(self.first_project, "first")

        def unavailable(_database_url: str):
            raise ConnectionError("postgres unavailable")

        target = PostgresWorkflowRepository("postgresql://redacted", connection_factory=unavailable)
        report = WorkflowMigrationService(self.repository, target).migrate(apply=False)
        self.assertGreater(report.source_count, 0)
        self.assertFalse(report.applied)
        with redirect_stdout(StringIO()):
            result = main([
                "migrate-workflow-postgres", "--database", str(self.database_path),
                "--database-url", "postgresql://preview-only",
            ])
        self.assertEqual(0, result)
        self.assertGreater(len(self.repository.list_migration_snapshots()), 0)


if __name__ == "__main__":
    unittest.main()
