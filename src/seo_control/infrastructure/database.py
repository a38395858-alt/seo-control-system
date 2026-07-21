"""SQLite schema bootstrap for the keyword-research MVP."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path


Migration = tuple[int, str, Sequence[str]]


_MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "keyword research foundation",
        (
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                site_url TEXT,
                default_country TEXT NOT NULL DEFAULT 'US',
                default_language TEXT NOT NULL DEFAULT 'en',
                default_currency TEXT,
                negative_terms_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS keyword_research_tasks (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                source_type TEXT NOT NULL CHECK (
                    source_type IN ('google_ads', 'google_suggest', 'file_import')
                ),
                mode TEXT,
                country_code TEXT NOT NULL,
                language_code TEXT NOT NULL,
                parameters_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'draft',
                discovered_count INTEGER NOT NULL DEFAULT 0,
                accepted_count INTEGER NOT NULL DEFAULT 0,
                rejected_count INTEGER NOT NULL DEFAULT 0,
                failure_reason TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                normalized_keyword TEXT NOT NULL,
                country_code TEXT NOT NULL,
                language_code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_review',
                priority INTEGER NOT NULL DEFAULT 0,
                opportunity_score REAL,
                first_discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                UNIQUE (project_id, normalized_keyword, country_code, language_code)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS import_batches (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                task_id INTEGER,
                original_filename TEXT NOT NULL,
                file_sha256 TEXT NOT NULL,
                mapping_json TEXT NOT NULL DEFAULT '{}',
                metric_date TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                total_rows INTEGER NOT NULL DEFAULT 0,
                accepted_rows INTEGER NOT NULL DEFAULT 0,
                rejected_rows INTEGER NOT NULL DEFAULT 0,
                error_file_reference TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (task_id) REFERENCES keyword_research_tasks(id) ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS suggest_query_jobs (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                normalized_query TEXT NOT NULL,
                hl TEXT NOT NULL,
                gl TEXT NOT NULL,
                expansion_rule TEXT NOT NULL DEFAULT 'seed',
                expansion_depth INTEGER NOT NULL DEFAULT 0,
                protocol_version TEXT NOT NULL DEFAULT 'google_suggest_v1',
                status TEXT NOT NULL DEFAULT 'queued',
                http_status INTEGER,
                response_time_ms INTEGER,
                raw_response_reference TEXT,
                error_code TEXT,
                attempted_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES keyword_research_tasks(id) ON DELETE CASCADE,
                UNIQUE (task_id, normalized_query, hl, gl, protocol_version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS keyword_sources (
                id INTEGER PRIMARY KEY,
                keyword_id INTEGER NOT NULL,
                source_type TEXT NOT NULL CHECK (
                    source_type IN ('google_ads', 'google_suggest', 'file_import')
                ),
                task_id INTEGER,
                import_batch_id INTEGER,
                seed_keyword TEXT,
                parent_query TEXT,
                expansion_rule TEXT,
                expansion_depth INTEGER,
                source_position INTEGER,
                raw_record_reference TEXT,
                discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE,
                FOREIGN KEY (task_id) REFERENCES keyword_research_tasks(id) ON DELETE SET NULL,
                FOREIGN KEY (import_batch_id) REFERENCES import_batches(id) ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS keyword_metric_snapshots (
                id INTEGER PRIMARY KEY,
                keyword_id INTEGER NOT NULL,
                source_type TEXT NOT NULL CHECK (
                    source_type IN ('google_ads', 'file_import')
                ),
                metric_date TEXT NOT NULL,
                country_code TEXT NOT NULL,
                language_code TEXT NOT NULL,
                geo_set_hash TEXT NOT NULL DEFAULT '',
                average_monthly_searches INTEGER,
                monthly_search_volumes_json TEXT,
                competition_level TEXT,
                competition_index INTEGER,
                low_top_of_page_bid_micros INTEGER,
                high_top_of_page_bid_micros INTEGER,
                currency_code TEXT,
                raw_record_reference TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE,
                UNIQUE (keyword_id, source_type, metric_date, geo_set_hash, language_code)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tasks_project ON keyword_research_tasks(project_id)",
            "CREATE INDEX IF NOT EXISTS idx_keywords_project ON keywords(project_id)",
            "CREATE INDEX IF NOT EXISTS idx_keyword_sources_keyword ON keyword_sources(keyword_id)",
            "CREATE INDEX IF NOT EXISTS idx_metric_snapshots_keyword ON keyword_metric_snapshots(keyword_id)",
            "CREATE INDEX IF NOT EXISTS idx_suggest_jobs_task ON suggest_query_jobs(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_import_batches_project ON import_batches(project_id)",
        ),
    ),
    (
        2,
        "import row audit",
        (
            """
            CREATE TABLE IF NOT EXISTS import_rows (
                id INTEGER PRIMARY KEY,
                import_batch_id INTEGER NOT NULL,
                row_number INTEGER NOT NULL,
                keyword TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                rejection_reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (import_batch_id) REFERENCES import_batches(id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_import_rows_batch ON import_rows(import_batch_id)",
        ),
    ),
    (
        3,
        "keyword library review and categorization",
        (
            "ALTER TABLE keywords ADD COLUMN deleted_at TEXT",
            "ALTER TABLE keywords ADD COLUMN demand_estimate INTEGER",
            """
            CREATE TABLE IF NOT EXISTS keyword_categories (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                UNIQUE(project_id, normalized_name)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS keyword_category_assignments (
                keyword_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'rule' CHECK(source IN ('rule', 'ai', 'manual')),
                confidence REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(keyword_id, category_id),
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE,
                FOREIGN KEY (category_id) REFERENCES keyword_categories(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS keyword_reviews (
                id INTEGER PRIMARY KEY,
                keyword_id INTEGER NOT NULL,
                seed_keyword TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'rule',
                is_seo_content_fit INTEGER NOT NULL,
                same_topic_as_seed INTEGER NOT NULL,
                search_intent TEXT,
                recommended_action TEXT,
                reason TEXT,
                confidence REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_keywords_active_project ON keywords(project_id, deleted_at)",
            "CREATE INDEX IF NOT EXISTS idx_keyword_reviews_keyword ON keyword_reviews(keyword_id, id DESC)",
        ),
    ),
    (
        4,
        "SEO title generation and selection",
        (
            """
            CREATE TABLE IF NOT EXISTS title_generation_jobs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                keyword_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
                request_json TEXT NOT NULL DEFAULT '{}',
                provider TEXT NOT NULL DEFAULT 'rule',
                model TEXT,
                prompt_version TEXT NOT NULL DEFAULT 'seo_title_us_v1',
                requested_count INTEGER NOT NULL DEFAULT 8,
                generated_count INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                error_summary TEXT,
                idempotency_key TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                completed_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE,
                UNIQUE(project_id, idempotency_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS keyword_title_candidates (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                keyword_id INTEGER NOT NULL,
                generation_job_id INTEGER,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                title_type TEXT,
                search_intent TEXT,
                reason TEXT,
                source_type TEXT NOT NULL CHECK(source_type IN ('ai', 'manual')),
                quality_score INTEGER NOT NULL DEFAULT 0,
                quality_details_json TEXT NOT NULL DEFAULT '{}',
                rule_version TEXT NOT NULL DEFAULT 'seo_title_us_v1',
                status TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate', 'selected', 'not_selected', 'archived')),
                selected_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                deleted_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE,
                FOREIGN KEY (generation_job_id) REFERENCES title_generation_jobs(id) ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS keyword_title_selection_events (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                keyword_id INTEGER NOT NULL,
                previous_candidate_id INTEGER,
                selected_candidate_id INTEGER,
                action TEXT NOT NULL CHECK(action IN ('selected', 'replaced', 'unselected')),
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE,
                FOREIGN KEY (previous_candidate_id) REFERENCES keyword_title_candidates(id) ON DELETE SET NULL,
                FOREIGN KEY (selected_candidate_id) REFERENCES keyword_title_candidates(id) ON DELETE SET NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_title_jobs_keyword ON title_generation_jobs(project_id, keyword_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_title_candidates_keyword ON keyword_title_candidates(project_id, keyword_id, created_at DESC)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_one_selected_title_per_keyword ON keyword_title_candidates(keyword_id) WHERE status='selected' AND deleted_at IS NULL",
        ),
    ),
    (
        5,
        "content system MVP",
        (
            """CREATE TABLE IF NOT EXISTS content_assets (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, keyword_id INTEGER NOT NULL, selected_title_candidate_id INTEGER NOT NULL, title_snapshot TEXT NOT NULL, locale TEXT NOT NULL DEFAULT 'en-US', country_code TEXT NOT NULL DEFAULT 'US', content_type TEXT NOT NULL DEFAULT 'guide', status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','briefing','outlining','drafting','in_review','needs_revision','approved','ready_to_publish','blocked','archived')), current_brief_id INTEGER, current_outline_id INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT, FOREIGN KEY(project_id) REFERENCES projects(id), FOREIGN KEY(keyword_id) REFERENCES keywords(id), FOREIGN KEY(selected_title_candidate_id) REFERENCES keyword_title_candidates(id))""",
            """CREATE TABLE IF NOT EXISTS content_briefs (id INTEGER PRIMARY KEY, content_asset_id INTEGER NOT NULL, target_audience TEXT NOT NULL, business_goal TEXT NOT NULL, target_length INTEGER NOT NULL, sources_json TEXT NOT NULL DEFAULT '[]', brief_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'current', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(content_asset_id) REFERENCES content_assets(id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS content_outlines (id INTEGER PRIMARY KEY, content_asset_id INTEGER NOT NULL, brief_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(content_asset_id) REFERENCES content_assets(id) ON DELETE CASCADE, FOREIGN KEY(brief_id) REFERENCES content_briefs(id))""",
            """CREATE TABLE IF NOT EXISTS content_outline_sections (id INTEGER PRIMARY KEY, outline_id INTEGER NOT NULL, position INTEGER NOT NULL, heading TEXT NOT NULL, purpose TEXT NOT NULL, word_budget INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(outline_id) REFERENCES content_outlines(id) ON DELETE CASCADE)""",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_content_asset_selected_title ON content_assets(project_id, selected_title_candidate_id) WHERE deleted_at IS NULL",
            "CREATE INDEX IF NOT EXISTS idx_content_assets_project ON content_assets(project_id, deleted_at, updated_at DESC)",
        ),
    ),
    (
        6,
        "staged AI content generation and version history",
        (
            "ALTER TABLE content_assets ADD COLUMN current_draft_id INTEGER",
            "ALTER TABLE content_assets ADD COLUMN current_generation_run_id INTEGER",
            """
            CREATE TABLE IF NOT EXISTS content_generation_runs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                content_asset_id INTEGER NOT NULL,
                stage TEXT NOT NULL CHECK(stage IN ('semantic','title','outline','section','assembly','qa')),
                provider TEXT NOT NULL,
                model TEXT,
                status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed','failed')),
                input_json TEXT NOT NULL DEFAULT '{}',
                output_json TEXT,
                error_summary TEXT,
                prompt_version TEXT NOT NULL DEFAULT 'content_synthesis_v1',
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(content_asset_id) REFERENCES content_assets(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS content_drafts (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                content_asset_id INTEGER NOT NULL,
                outline_id INTEGER,
                generation_run_id INTEGER,
                version INTEGER NOT NULL,
                title TEXT NOT NULL,
                meta_description TEXT,
                markdown TEXT NOT NULL,
                sources_used_json TEXT NOT NULL DEFAULT '[]',
                unresolved_verify_json TEXT NOT NULL DEFAULT '[]',
                qa_json TEXT NOT NULL DEFAULT '{}',
                qa_status TEXT NOT NULL DEFAULT 'needs_verification',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(content_asset_id) REFERENCES content_assets(id) ON DELETE CASCADE,
                FOREIGN KEY(outline_id) REFERENCES content_outlines(id) ON DELETE SET NULL,
                FOREIGN KEY(generation_run_id) REFERENCES content_generation_runs(id) ON DELETE SET NULL,
                UNIQUE(content_asset_id, version)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_content_runs_asset ON content_generation_runs(content_asset_id, id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_content_drafts_asset ON content_drafts(content_asset_id, version DESC)",
        ),
    ),
    (
        7,
        "content draft provider audit fields",
        (
            "ALTER TABLE content_drafts ADD COLUMN provider TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE content_drafts ADD COLUMN model TEXT",
        ),
    ),
    (
        8,
        "backfill completed content workflow status",
        (
            "UPDATE content_assets SET status='needs_revision', updated_at=CURRENT_TIMESTAMP WHERE current_draft_id IS NOT NULL AND status IN ('planned','briefing','outlining','drafting')",
        ),
    ),
    (
        9,
        "content generation provider lock jobs",
        (
            """
            CREATE TABLE IF NOT EXISTS content_generation_jobs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                content_asset_id INTEGER NOT NULL,
                requested_action TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT,
                status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed','failed')),
                failed_stage TEXT,
                error_summary TEXT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(content_asset_id) REFERENCES content_assets(id) ON DELETE CASCADE
            )
            """,
            "ALTER TABLE content_generation_runs ADD COLUMN generation_job_id INTEGER",
            "ALTER TABLE content_drafts ADD COLUMN generation_job_id INTEGER",
            "CREATE INDEX IF NOT EXISTS idx_content_generation_jobs_asset ON content_generation_jobs(content_asset_id, id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_content_runs_job ON content_generation_runs(generation_job_id, id)",
        ),
    ),
    (
        10,
        "content outline section blueprint preservation",
        (
            "ALTER TABLE content_outline_sections ADD COLUMN section_json TEXT NOT NULL DEFAULT '{}'",
        ),
    ),
    (
        11,
        "project-scoped SERP title learning memory",
        (
            """
            CREATE TABLE IF NOT EXISTS serp_title_samples (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                keyword_id INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                source TEXT,
                source_type TEXT NOT NULL CHECK(source_type IN ('browser', 'ai')),
                locale TEXT NOT NULL DEFAULT 'en-US',
                captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(keyword_id) REFERENCES keywords(id) ON DELETE CASCADE,
                UNIQUE(project_id, keyword_id, normalized_title)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_serp_title_samples_keyword ON serp_title_samples(project_id, keyword_id, captured_at DESC)",
        ),
    ),
    (
        12,
        "website-scoped competitor content learning memory",
        (
            """
            CREATE TABLE IF NOT EXISTS competitor_research_runs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                content_asset_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                locale TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed','insufficient','failed')),
                discovered_count INTEGER NOT NULL DEFAULT 0,
                usable_count INTEGER NOT NULL DEFAULT 0,
                analysis_json TEXT NOT NULL DEFAULT '{}',
                error_summary TEXT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(content_asset_id) REFERENCES content_assets(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS competitor_content_memory (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                normalized_url TEXT NOT NULL,
                url TEXT NOT NULL,
                domain TEXT NOT NULL,
                page_title TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                structure_json TEXT NOT NULL DEFAULT '{}',
                first_captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                UNIQUE(project_id, normalized_url)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS competitor_research_items (
                id INTEGER PRIMARY KEY,
                research_run_id INTEGER NOT NULL,
                memory_id INTEGER,
                rank INTEGER NOT NULL,
                search_title TEXT NOT NULL,
                url TEXT NOT NULL,
                domain TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('selected','skipped','failed')),
                error_summary TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(research_run_id) REFERENCES competitor_research_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(memory_id) REFERENCES competitor_content_memory(id) ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS competitor_content_chunks (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                memory_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                content TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(memory_id) REFERENCES competitor_content_memory(id) ON DELETE CASCADE,
                UNIQUE(memory_id, position)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_competitor_memory_project ON competitor_content_memory(project_id,last_captured_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_competitor_chunks_project ON competitor_content_chunks(project_id,memory_id)",
            "CREATE INDEX IF NOT EXISTS idx_competitor_runs_asset ON competitor_research_runs(content_asset_id,id DESC)",
        ),
    ),
    (
        13,
        "project-scoped authority source library",
        (
            """
            CREATE TABLE IF NOT EXISTS authority_source_library (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN ('first_party','standard','certification','government','industry_research')),
                url TEXT,
                publisher TEXT,
                published_at TEXT,
                content TEXT NOT NULL,
                authority_level TEXT NOT NULL DEFAULT 'needs_review' CHECK(authority_level IN ('primary','authoritative','supporting','needs_review')),
                tags_json TEXT NOT NULL DEFAULT '[]',
                classification_json TEXT NOT NULL DEFAULT '{}',
                summary TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_authority_sources_project ON authority_source_library(project_id, updated_at DESC)",
        ),
    ),
    (
        14,
        "article-scoped authority source references",
        (
            """
            CREATE TABLE IF NOT EXISTS content_authority_source_links (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                content_asset_id INTEGER NOT NULL,
                authority_source_id INTEGER NOT NULL,
                section_heading TEXT,
                claim_topic TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(content_asset_id) REFERENCES content_assets(id) ON DELETE CASCADE,
                FOREIGN KEY(authority_source_id) REFERENCES authority_source_library(id) ON DELETE CASCADE,
                UNIQUE(content_asset_id, authority_source_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_article_authority_sources ON content_authority_source_links(project_id, content_asset_id)",
        ),
    ),
)


def initialize_database(database_path: str | Path) -> sqlite3.Connection:
    """Open a database, apply pending migrations, and enable foreign keys."""

    path = _as_database_path(database_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        _apply_migrations(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _as_database_path(database_path: str | Path) -> str:
    if isinstance(database_path, Path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        return str(database_path)
    if isinstance(database_path, str):
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        return database_path
    raise TypeError("database_path must be a str or pathlib.Path")


def _apply_migrations(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        applied_versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        for version, name, statements in _MIGRATIONS:
            if version in applied_versions:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (version, name),
            )
