"""PostgreSQL storage for the source-driven B2B Agent platform."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from psycopg import connect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


FACT_PROMISE_TERMS = ("best", "compare", "comparison", "pricing", "price", "free", "features", "review", "certification", "performance", "size", "dimension", "recommend")
STRUCTURED_SECTION_TERMS = ("compare", "price", "pricing", "feature", "spec", "parameter", "risk", "certif", "qualification", "cost")


def connection():
    return connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def initialize_platform_schema() -> None:
    """Idempotent P0/P1 schema; existing Docker volumes are upgraded in place."""
    with connection() as database, database.cursor() as cursor:
        cursor.execute("""CREATE TABLE IF NOT EXISTS websites (id BIGSERIAL PRIMARY KEY,domain TEXT NOT NULL UNIQUE,industry TEXT NOT NULL,audience TEXT NOT NULL DEFAULT '',brand_tone TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        for statement in (
            "ALTER TABLE websites ADD COLUMN IF NOT EXISTS country_code TEXT NOT NULL DEFAULT 'US'",
            "ALTER TABLE websites ADD COLUMN IF NOT EXISTS language_code TEXT NOT NULL DEFAULT 'en-US'",
            "ALTER TABLE websites ADD COLUMN IF NOT EXISTS product_scope TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE websites ADD COLUMN IF NOT EXISTS prohibited_claims JSONB NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE websites ADD COLUMN IF NOT EXISTS is_legacy BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE websites ADD COLUMN IF NOT EXISTS domain_status TEXT NOT NULL DEFAULT 'verified'",
            "ALTER TABLE websites ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            "ALTER TABLE websites ADD COLUMN IF NOT EXISTS workspace_project_id BIGINT",
        ):
            cursor.execute(statement)
        # The legacy SQLite workbench stores its business data by project_id.
        # A website must therefore own exactly one distinct workspace project:
        # sharing one project between sites would mix keywords, titles, drafts
        # and SERP-learning samples.  NULL remains valid only for legacy sites
        # that have not yet been connected to the workbench.
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_websites_workspace_project "
            "ON websites(workspace_project_id) WHERE workspace_project_id IS NOT NULL"
        )
        cursor.execute("""CREATE TABLE IF NOT EXISTS site_terms (id BIGSERIAL PRIMARY KEY,site_id BIGINT NOT NULL REFERENCES websites(id) ON DELETE CASCADE,term TEXT NOT NULL,kind TEXT NOT NULL,definition TEXT NOT NULL DEFAULT '',source_note TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(site_id,term,kind))""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS site_facts (id BIGSERIAL PRIMARY KEY,site_id BIGINT NOT NULL REFERENCES websites(id) ON DELETE CASCADE,title TEXT NOT NULL,kind TEXT NOT NULL,detail TEXT NOT NULL,source_note TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS site_knowledge_documents (id BIGSERIAL PRIMARY KEY,site_id BIGINT NOT NULL REFERENCES websites(id) ON DELETE CASCADE,title TEXT NOT NULL,source_type TEXT NOT NULL,url TEXT NOT NULL DEFAULT '',content TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'ready',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(site_id,title))""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS seo_tasks (id BIGSERIAL PRIMARY KEY,website_id BIGINT NOT NULL REFERENCES websites(id) ON DELETE CASCADE,target_keyword TEXT NOT NULL,title TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'queued',stage TEXT NOT NULL DEFAULT 'task_initialization',progress SMALLINT NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),message TEXT NOT NULL DEFAULT '',celery_task_id TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        for statement in (
            "ALTER TABLE seo_tasks ADD COLUMN IF NOT EXISTS task_type TEXT NOT NULL DEFAULT 'content'",
            "ALTER TABLE seo_tasks ADD COLUMN IF NOT EXISTS title_promises JSONB NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE seo_tasks ADD COLUMN IF NOT EXISTS source_sufficiency JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE seo_tasks ADD COLUMN IF NOT EXISTS failure_reason TEXT NOT NULL DEFAULT ''",
        ):
            cursor.execute(statement)
        cursor.execute("""CREATE TABLE IF NOT EXISTS task_runs (id BIGSERIAL PRIMARY KEY,task_id BIGINT NOT NULL REFERENCES seo_tasks(id) ON DELETE CASCADE,stage TEXT NOT NULL,status TEXT NOT NULL,message TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS source_packs (id BIGSERIAL PRIMARY KEY,task_id BIGINT NOT NULL UNIQUE REFERENCES seo_tasks(id) ON DELETE CASCADE,site_id BIGINT NOT NULL REFERENCES websites(id) ON DELETE CASCADE,name TEXT NOT NULL DEFAULT 'Manual source pack',research_status TEXT NOT NULL DEFAULT 'manual_only',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS source_items (id BIGSERIAL PRIMARY KEY,source_pack_id BIGINT NOT NULL REFERENCES source_packs(id) ON DELETE CASCADE,site_id BIGINT NOT NULL REFERENCES websites(id) ON DELETE CASCADE,source_type TEXT NOT NULL,label TEXT NOT NULL,url TEXT NOT NULL DEFAULT '',publisher TEXT NOT NULL DEFAULT '',published_at TEXT NOT NULL DEFAULT '',availability TEXT NOT NULL DEFAULT 'available',content TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS outlines (id BIGSERIAL PRIMARY KEY,task_id BIGINT NOT NULL UNIQUE REFERENCES seo_tasks(id) ON DELETE CASCADE,site_id BIGINT NOT NULL REFERENCES websites(id) ON DELETE CASCADE,status TEXT NOT NULL DEFAULT 'draft',confirmed_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS outline_sections (id BIGSERIAL PRIMARY KEY,outline_id BIGINT NOT NULL REFERENCES outlines(id) ON DELETE CASCADE,position SMALLINT NOT NULL,heading TEXT NOT NULL,reader_question TEXT NOT NULL,purpose TEXT NOT NULL,key_points JSONB NOT NULL DEFAULT '[]'::jsonb,source_item_ids JSONB NOT NULL DEFAULT '[]'::jsonb,format TEXT NOT NULL,title_promise TEXT NOT NULL DEFAULT '',UNIQUE(outline_id,position))""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS legacy_import_runs (id BIGSERIAL PRIMARY KEY,legacy_project_id BIGINT NOT NULL UNIQUE,site_id BIGINT REFERENCES websites(id) ON DELETE SET NULL,status TEXT NOT NULL DEFAULT 'queued',counts JSONB NOT NULL DEFAULT '{}'::jsonb,error_summary TEXT NOT NULL DEFAULT '',started_at TIMESTAMPTZ,completed_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS legacy_records (id BIGSERIAL PRIMARY KEY,site_id BIGINT NOT NULL REFERENCES websites(id) ON DELETE CASCADE,import_run_id BIGINT NOT NULL REFERENCES legacy_import_runs(id) ON DELETE CASCADE,record_type TEXT NOT NULL,legacy_id BIGINT NOT NULL,payload JSONB NOT NULL,origin_created_at TEXT NOT NULL DEFAULT '',source_label TEXT NOT NULL DEFAULT 'legacy_sqlite',is_read_only BOOLEAN NOT NULL DEFAULT TRUE,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(site_id,record_type,legacy_id))""")
        for statement in (
            "ALTER TABLE legacy_records ADD COLUMN IF NOT EXISTS source_label TEXT NOT NULL DEFAULT 'legacy_sqlite'",
            "ALTER TABLE legacy_records ADD COLUMN IF NOT EXISTS is_read_only BOOLEAN NOT NULL DEFAULT TRUE",
        ):
            cursor.execute(statement)


def _rows(cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _one(cursor) -> dict[str, Any]:
    row = cursor.fetchone()
    if row is None:
        raise ValueError("record does not exist")
    return dict(row)


def _site(cursor, site_id: int) -> dict[str, Any]:
    cursor.execute("SELECT * FROM websites WHERE id=%s", (site_id,))
    return _one(cursor)


def _task(cursor, task_id: int, site_id: int | None = None) -> dict[str, Any]:
    query = "SELECT tasks.*,websites.domain,websites.industry FROM seo_tasks tasks JOIN websites ON websites.id=tasks.website_id WHERE tasks.id=%s"
    values: list[Any] = [task_id]
    if site_id is not None:
        query += " AND tasks.website_id=%s"
        values.append(site_id)
    cursor.execute(query, values)
    return _one(cursor)


def _title_promises(title: str) -> list[str]:
    lowered = title.lower()
    return [term for term in FACT_PROMISE_TERMS if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lowered)]


def _task_run(cursor, task_id: int, stage: str, status: str, message: str) -> None:
    cursor.execute("INSERT INTO task_runs(task_id,stage,status,message) VALUES(%s,%s,%s,%s)", (task_id, stage, status, message))


def list_websites() -> list[dict[str, Any]]:
    with connection() as database, database.cursor() as cursor:
        cursor.execute("SELECT * FROM websites ORDER BY is_legacy, id DESC")
        return _rows(cursor)


def create_website(**value: Any) -> dict[str, Any]:
    with connection() as database, database.cursor() as cursor:
        cursor.execute(
            """INSERT INTO websites(domain,industry,audience,brand_tone,country_code,language_code,product_scope,prohibited_claims,is_legacy,domain_status,workspace_project_id)
               VALUES(%(domain)s,%(industry)s,%(audience)s,%(brand_tone)s,%(country_code)s,%(language_code)s,%(product_scope)s,%(prohibited_claims)s,%(is_legacy)s,%(domain_status)s,%(workspace_project_id)s) RETURNING *""",
            value | {"prohibited_claims": Jsonb(value.get("prohibited_claims", [])), "workspace_project_id": value.get("workspace_project_id")},
        )
        return _one(cursor)


def update_website(site_id: int, updates: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"domain", "industry", "audience", "brand_tone", "country_code", "language_code", "product_scope", "prohibited_claims", "domain_status", "workspace_project_id"}
    values = {key: value for key, value in updates.items() if key in allowed and value is not None}
    if not values:
        raise ValueError("no editable fields supplied")
    if "prohibited_claims" in values:
        values["prohibited_claims"] = Jsonb(values["prohibited_claims"])
    assignments = ",".join(f"{key}=%({key})s" for key in values)
    with connection() as database, database.cursor() as cursor:
        if "workspace_project_id" in values:
            cursor.execute("SELECT workspace_project_id FROM websites WHERE id=%s", (site_id,))
            current = cursor.fetchone()
            if current is None:
                raise ValueError("website does not exist")
            existing_project_id = current["workspace_project_id"]
            requested_project_id = values["workspace_project_id"]
            if existing_project_id is not None and requested_project_id != existing_project_id:
                raise ValueError("website workspace project is permanent and cannot be reassigned")
        values["id"] = site_id
        cursor.execute(f"UPDATE websites SET {assignments},updated_at=NOW() WHERE id=%(id)s RETURNING *", values)
        return _one(cursor)


def delete_website(site_id: int) -> None:
    """Delete a workspace and its task-local data.

    This is primarily useful for disposable workspaces and integration-test
    cleanup.  Legacy runs intentionally keep their audit row with ``site_id``
    cleared by the database's foreign-key rule.
    """
    with connection() as database, database.cursor() as cursor:
        cursor.execute("DELETE FROM websites WHERE id=%s", (site_id,))
        if cursor.rowcount != 1:
            raise ValueError("website does not exist")


def list_knowledge(site_id: int) -> list[dict[str, Any]]:
    with connection() as database, database.cursor() as cursor:
        _site(cursor, site_id)
        cursor.execute("SELECT * FROM site_knowledge_documents WHERE site_id=%s ORDER BY id DESC", (site_id,))
        return _rows(cursor)


def add_knowledge(site_id: int, value: Mapping[str, str]) -> dict[str, Any]:
    with connection() as database, database.cursor() as cursor:
        _site(cursor, site_id)
        cursor.execute("INSERT INTO site_knowledge_documents(site_id,title,source_type,url,content,status) VALUES(%s,%s,%s,%s,%s,'ready') RETURNING *", (site_id, value["title"], value["source_type"], value.get("url", ""), value.get("content", "")))
        return _one(cursor)


def delete_knowledge(site_id: int, document_id: int) -> None:
    with connection() as database, database.cursor() as cursor:
        cursor.execute("DELETE FROM site_knowledge_documents WHERE id=%s AND site_id=%s", (document_id, site_id))
        if cursor.rowcount != 1:
            raise ValueError("knowledge document does not exist")


def knowledge_context(site_id: int) -> dict[str, Any]:
    documents = list_knowledge(site_id)
    content = "\n\n---\n\n".join(f"[Knowledge: {item['title']}]\n{item['content']}" for item in reversed(documents) if item["status"] == "ready" and item["content"])
    return {"site_id": site_id, "documents": documents, "content": content}


def list_site_items(site_id: int, table: str) -> list[dict[str, Any]]:
    if table not in {"site_terms", "site_facts"}:
        raise ValueError("unsupported site item table")
    with connection() as database, database.cursor() as cursor:
        _site(cursor, site_id)
        cursor.execute(f"SELECT * FROM {table} WHERE site_id=%s ORDER BY id DESC", (site_id,))
        return _rows(cursor)


def create_site_item(site_id: int, table: str, value: Mapping[str, str]) -> dict[str, Any]:
    with connection() as database, database.cursor() as cursor:
        _site(cursor, site_id)
        if table == "site_terms":
            cursor.execute("INSERT INTO site_terms(site_id,term,kind,definition,source_note) VALUES(%s,%s,%s,%s,%s) RETURNING *", (site_id, value["term"], value["kind"], value.get("definition", ""), value.get("source_note", "")))
        elif table == "site_facts":
            cursor.execute("INSERT INTO site_facts(site_id,title,kind,detail,source_note) VALUES(%s,%s,%s,%s,%s) RETURNING *", (site_id, value["title"], value["kind"], value["detail"], value.get("source_note", "")))
        else:
            raise ValueError("unsupported site item table")
        return _one(cursor)


def delete_site_item(site_id: int, table: str, item_id: int) -> None:
    if table not in {"site_terms", "site_facts"}:
        raise ValueError("unsupported site item table")
    with connection() as database, database.cursor() as cursor:
        cursor.execute(f"DELETE FROM {table} WHERE id=%s AND site_id=%s", (item_id, site_id))
        if cursor.rowcount != 1:
            raise ValueError("site item does not exist")


def create_task(*, website_id: int, target_keyword: str, title: str, task_type: str = "content") -> dict[str, Any]:
    with connection() as database, database.cursor() as cursor:
        _site(cursor, website_id)
        promises = _title_promises(title)
        cursor.execute(
            """INSERT INTO seo_tasks(website_id,target_keyword,title,task_type,title_promises,message)
               VALUES(%s,%s,%s,%s,%s,%s) RETURNING *""",
            (website_id, target_keyword, title, task_type, Jsonb(promises), "Task queued: waiting for the worker to initialize source requirements."),
        )
        task = _one(cursor)
        _task_run(cursor, int(task["id"]), "task_initialization", "queued", task["message"])
        return task


def set_task_celery_id(task_id: int, celery_task_id: str) -> None:
    with connection() as database, database.cursor() as cursor:
        cursor.execute("UPDATE seo_tasks SET celery_task_id=%s,updated_at=NOW() WHERE id=%s", (celery_task_id, task_id))


def initialize_task(task_id: int) -> None:
    with connection() as database, database.cursor() as cursor:
        task = _task(cursor, task_id)
        message = "Source pack is required before SERP analysis and writing can begin."
        cursor.execute("UPDATE seo_tasks SET status='waiting_for_sources',stage='source_pack',progress=15,message=%s,updated_at=NOW() WHERE id=%s", (message, task_id))
        _task_run(cursor, task_id, "source_pack", "waiting_for_sources", message)


def record_task_failure(task_id: int, stage: str, error: Exception) -> None:
    """Keep worker failures observable without inventing a successful stage."""
    message = str(error) or "background task failed"
    with connection() as database, database.cursor() as cursor:
        _task(cursor, task_id)
        cursor.execute(
            "UPDATE seo_tasks SET status='failed',stage=%s,message=%s,failure_reason=%s,updated_at=NOW() WHERE id=%s",
            (stage, message, message, task_id),
        )
        _task_run(cursor, task_id, stage, "failed", message)


def _source_pack(cursor, task: Mapping[str, Any]) -> int:
    cursor.execute("INSERT INTO source_packs(task_id,site_id) VALUES(%s,%s) ON CONFLICT(task_id) DO UPDATE SET task_id=EXCLUDED.task_id RETURNING id", (task["id"], task["website_id"]))
    return int(cursor.fetchone()["id"])


def add_source(task_id: int, value: Mapping[str, str], site_id: int | None = None) -> dict[str, Any]:
    with connection() as database, database.cursor() as cursor:
        task = _task(cursor, task_id, site_id)
        pack_id = _source_pack(cursor, task)
        cursor.execute(
            """INSERT INTO source_items(source_pack_id,site_id,source_type,label,url,publisher,published_at,availability,content)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (pack_id, task["website_id"], value["source_type"], value["label"], value.get("url", ""), value.get("publisher", ""), value.get("published_at", ""), value.get("availability", "available"), value.get("content", "")),
        )
        return _one(cursor)


def list_sources(task_id: int, site_id: int | None = None) -> list[dict[str, Any]]:
    with connection() as database, database.cursor() as cursor:
        task = _task(cursor, task_id, site_id)
        cursor.execute("SELECT items.* FROM source_items items JOIN source_packs packs ON packs.id=items.source_pack_id WHERE packs.task_id=%s ORDER BY items.id", (task["id"],))
        return _rows(cursor)


def delete_source(task_id: int, item_id: int, site_id: int | None = None) -> None:
    with connection() as database, database.cursor() as cursor:
        task = _task(cursor, task_id, site_id)
        cursor.execute("DELETE FROM source_items WHERE id=%s AND site_id=%s AND source_pack_id=(SELECT id FROM source_packs WHERE task_id=%s)", (item_id, task["website_id"], task_id))
        if cursor.rowcount != 1:
            raise ValueError("source does not exist for this task")


def assess_sources(task_id: int) -> dict[str, Any]:
    with connection() as database, database.cursor() as cursor:
        task = _task(cursor, task_id)
        cursor.execute("SELECT COUNT(*) AS usable FROM source_items items JOIN source_packs packs ON packs.id=items.source_pack_id WHERE packs.task_id=%s AND items.availability='available' AND (items.content<>'' OR items.url<>'')", (task_id,))
        usable = int(cursor.fetchone()["usable"])
        promises = list(task["title_promises"] or [])
        required = 2 if promises else 1
        sufficient = usable >= required
        status = "ready_for_outline" if sufficient else "waiting_for_sources"
        stage = "outline" if sufficient else "source_pack"
        message = "Sources are sufficient for a manually edited outline." if sufficient else f"{required} usable source(s) required for this title; only {usable} available."
        report = {"usable_sources": usable, "required_sources": required, "title_promises": promises, "sufficient": sufficient, "serp_research": "not_configured"}
        cursor.execute("UPDATE seo_tasks SET status=%s,stage=%s,progress=%s,message=%s,source_sufficiency=%s,updated_at=NOW() WHERE id=%s", (status, stage, 30 if sufficient else 15, message, Jsonb(report), task_id))
        _task_run(cursor, task_id, "source_assessment", status, message)
        return report | {"status": status}


def _validate_sections(cursor, task: Mapping[str, Any], sections: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    purposes: set[str] = set()
    for position, section in enumerate(sections, 1):
        heading, question, purpose = (str(section.get(key, "")).strip() for key in ("heading", "reader_question", "purpose"))
        if not heading or not question or not purpose:
            raise ValueError("each outline section needs heading, reader_question, and purpose")
        key = " ".join(purpose.lower().split())
        if key in purposes:
            raise ValueError("outline section purposes must be unique")
        purposes.add(key)
        source_ids = [int(item) for item in section.get("source_item_ids", [])]
        if not source_ids:
            raise ValueError("each outline section needs at least one task source")
        cursor.execute("""SELECT COUNT(*) AS count FROM source_items items
                          JOIN source_packs packs ON packs.id=items.source_pack_id
                          WHERE items.site_id=%s AND packs.task_id=%s AND items.id=ANY(%s)""", (task["website_id"], task["id"], source_ids))
        if int(cursor.fetchone()["count"]) != len(set(source_ids)):
            raise ValueError("outline section references a source from another task or site")
        format_value = str(section.get("format", "")).strip()
        if format_value not in {"paragraphs", "list", "table"}:
            raise ValueError("outline section format must be paragraphs, list, or table")
        text = f"{heading} {purpose}".lower()
        if any(term in text for term in STRUCTURED_SECTION_TERMS) and format_value == "paragraphs":
            raise ValueError("comparison, price, feature, parameter, risk, or qualification sections require table or list format")
        normalized.append({"position": position, "heading": heading, "reader_question": question, "purpose": purpose, "key_points": [str(point).strip() for point in section.get("key_points", []) if str(point).strip()], "source_item_ids": source_ids, "format": format_value, "title_promise": str(section.get("title_promise", "")).strip()})
    if not 5 <= len(normalized) <= 8:
        raise ValueError("an outline requires 5 to 8 H2 sections")
    return normalized


def save_outline(task_id: int, sections: Iterable[Mapping[str, Any]], site_id: int | None = None) -> dict[str, Any]:
    with connection() as database, database.cursor() as cursor:
        task = _task(cursor, task_id, site_id)
        if task["status"] not in {"ready_for_outline", "outline_pending_confirmation"}:
            raise ValueError("sources must be sufficient before an outline can be saved")
        prepared = _validate_sections(cursor, task, sections)
        cursor.execute("INSERT INTO outlines(task_id,site_id,status) VALUES(%s,%s,'draft') ON CONFLICT(task_id) DO UPDATE SET status='draft',updated_at=NOW() RETURNING id", (task_id, task["website_id"]))
        outline_id = int(cursor.fetchone()["id"])
        cursor.execute("DELETE FROM outline_sections WHERE outline_id=%s", (outline_id,))
        for section in prepared:
            cursor.execute("""INSERT INTO outline_sections(outline_id,position,heading,reader_question,purpose,key_points,source_item_ids,format,title_promise)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (outline_id, section["position"], section["heading"], section["reader_question"], section["purpose"], Jsonb(section["key_points"]), Jsonb(section["source_item_ids"]), section["format"], section["title_promise"]))
        message = "Outline is ready for your confirmation; no body content has been generated."
        cursor.execute("UPDATE seo_tasks SET status='outline_pending_confirmation',stage='outline_confirmation',progress=40,message=%s,updated_at=NOW() WHERE id=%s", (message, task_id))
        _task_run(cursor, task_id, "outline", "outline_pending_confirmation", message)
        return get_outline(task_id)


def get_outline(task_id: int, site_id: int | None = None) -> dict[str, Any]:
    with connection() as database, database.cursor() as cursor:
        task = _task(cursor, task_id, site_id)
        cursor.execute("SELECT * FROM outlines WHERE task_id=%s", (task_id,))
        outline = cursor.fetchone()
        if outline is None:
            return {"task_id": task_id, "status": "missing", "sections": []}
        value = dict(outline)
        cursor.execute("SELECT * FROM outline_sections WHERE outline_id=%s ORDER BY position", (outline["id"],))
        value["sections"] = _rows(cursor)
        return value


def confirm_outline(task_id: int, site_id: int | None = None) -> dict[str, Any]:
    with connection() as database, database.cursor() as cursor:
        task = _task(cursor, task_id, site_id)
        cursor.execute("SELECT id FROM outlines WHERE task_id=%s AND status='draft'", (task_id,))
        outline = cursor.fetchone()
        if outline is None:
            raise ValueError("a draft outline is required before confirmation")
        cursor.execute("UPDATE outlines SET status='confirmed',confirmed_at=NOW(),updated_at=NOW() WHERE id=%s", (outline["id"],))
        message = "Outline confirmed. H2 writing remains disabled until P2 is implemented."
        cursor.execute("UPDATE seo_tasks SET status='outline_confirmed',stage='outline_confirmed',progress=45,message=%s,updated_at=NOW() WHERE id=%s", (message, task_id))
        _task_run(cursor, task_id, "outline_confirmation", "outline_confirmed", message)
        return get_task_detail(task_id)


def get_task_detail(task_id: int, site_id: int | None = None) -> dict[str, Any]:
    with connection() as database, database.cursor() as cursor:
        task = _task(cursor, task_id, site_id)
        cursor.execute("SELECT * FROM task_runs WHERE task_id=%s ORDER BY id", (task_id,))
        runs = _rows(cursor)
    return {"task": task, "sources": list_sources(task_id, site_id), "outline": get_outline(task_id, site_id), "runs": runs}


def list_tasks() -> list[dict[str, Any]]:
    with connection() as database, database.cursor() as cursor:
        cursor.execute("SELECT tasks.*,websites.domain,websites.industry FROM seo_tasks tasks JOIN websites ON websites.id=tasks.website_id ORDER BY tasks.id DESC LIMIT 50")
        return _rows(cursor)


def _legacy_sqlite_path() -> Path:
    path = Path(os.getenv("LEGACY_SQLITE_PATH", "/legacy-data/seo-control.sqlite3"))
    if not path.exists():
        raise ValueError("legacy SQLite database is not mounted")
    return path


def preview_legacy_import() -> dict[str, Any]:
    source = sqlite3.connect(_legacy_sqlite_path())
    source.row_factory = sqlite3.Row
    try:
        projects = [dict(row) for row in source.execute("SELECT id,name,site_url,default_country,default_language FROM projects ORDER BY id")]
        for project in projects:
            project_id = project["id"]
            project["counts"] = {table: source.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id=?", (project_id,)).fetchone()[0] for table in ("keywords", "keyword_title_candidates", "content_assets")}
            project["counts"]["content_drafts"] = source.execute("SELECT COUNT(*) FROM content_drafts drafts JOIN content_assets assets ON assets.id=drafts.content_asset_id WHERE assets.project_id=?", (project_id,)).fetchone()[0]
        return {"projects": projects, "source": str(_legacy_sqlite_path())}
    finally:
        source.close()


def queue_legacy_import_runs() -> list[dict[str, Any]]:
    projects = preview_legacy_import()["projects"]
    queued: list[dict[str, Any]] = []
    with connection() as database, database.cursor() as cursor:
        for project in projects:
            cursor.execute("INSERT INTO legacy_import_runs(legacy_project_id,status) VALUES(%s,'queued') ON CONFLICT(legacy_project_id) DO UPDATE SET status=CASE WHEN legacy_import_runs.status='completed' THEN 'completed' ELSE 'queued' END RETURNING *", (project["id"],))
            queued.append(_one(cursor))
    return queued


def _sqlite_records(source: sqlite3.Connection, project_id: int, table: str) -> list[dict[str, Any]]:
    if table == "content_drafts":
        query = "SELECT drafts.* FROM content_drafts drafts JOIN content_assets assets ON assets.id=drafts.content_asset_id WHERE assets.project_id=?"
    else:
        query = f"SELECT * FROM {table} WHERE project_id=?"
    return [dict(row) for row in source.execute(query, (project_id,))]


def run_legacy_import(legacy_project_id: int) -> dict[str, Any]:
    source = sqlite3.connect(_legacy_sqlite_path())
    source.row_factory = sqlite3.Row
    try:
        project = source.execute("SELECT * FROM projects WHERE id=?", (legacy_project_id,)).fetchone()
        if project is None:
            raise ValueError("legacy project does not exist")
        project_data = dict(project)
        with connection() as database, database.cursor() as cursor:
            domain = f"legacy-project-{legacy_project_id}.local"
            cursor.execute("""INSERT INTO websites(domain,industry,audience,brand_tone,country_code,language_code,product_scope,prohibited_claims,is_legacy,domain_status)
                           VALUES(%s,%s,'', '', %s, %s, '', '[]'::jsonb,TRUE,'needs_domain')
                           ON CONFLICT(domain) DO UPDATE SET updated_at=NOW() RETURNING id""", (domain, f"Historical project: {project_data.get('name') or legacy_project_id}", project_data.get("default_country") or "US", project_data.get("default_language") or "en-US"))
            site_id = int(cursor.fetchone()["id"])
            cursor.execute("INSERT INTO legacy_import_runs(legacy_project_id,site_id,status,started_at,error_summary) VALUES(%s,%s,'running',NOW(),'') ON CONFLICT(legacy_project_id) DO UPDATE SET site_id=EXCLUDED.site_id,status='running',started_at=NOW(),error_summary='' RETURNING id", (legacy_project_id, site_id))
            run_id = int(cursor.fetchone()["id"])
            counts: dict[str, int] = {}
            for record_type in ("keywords", "keyword_title_candidates", "content_assets", "content_drafts"):
                records = _sqlite_records(source, legacy_project_id, record_type)
                # Counts shown to the user describe the historical project, not
                # only this idempotent re-run (which may insert zero rows).
                counts[record_type] = len(records)
                for record in records:
                    legacy_id = int(record["id"])
                    origin = str(record.get("created_at") or "")
                    cursor.execute("""INSERT INTO legacy_records(site_id,import_run_id,record_type,legacy_id,payload,origin_created_at)
                                   VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(site_id,record_type,legacy_id) DO NOTHING""", (site_id, run_id, record_type, legacy_id, Jsonb(record), origin))
            cursor.execute("UPDATE legacy_import_runs SET status='completed',counts=%s,completed_at=NOW() WHERE id=%s", (Jsonb(counts), run_id))
            return {"legacy_project_id": legacy_project_id, "site_id": site_id, "status": "completed", "counts": counts}
    except Exception as error:
        with connection() as database, database.cursor() as cursor:
            cursor.execute("UPDATE legacy_import_runs SET status='failed',error_summary=%s,completed_at=NOW() WHERE legacy_project_id=%s", (str(error), legacy_project_id))
        raise
    finally:
        source.close()


def list_legacy_imports() -> list[dict[str, Any]]:
    with connection() as database, database.cursor() as cursor:
        cursor.execute("SELECT runs.*,websites.domain FROM legacy_import_runs runs LEFT JOIN websites ON websites.id=runs.site_id ORDER BY legacy_project_id")
        return _rows(cursor)


def dashboard_summary() -> dict[str, Any]:
    with connection() as database, database.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS websites FROM websites")
        website_count = cursor.fetchone()["websites"]
        cursor.execute("SELECT COUNT(*) AS tasks,COUNT(*) FILTER (WHERE status='waiting_for_sources') AS waiting_for_sources,COUNT(*) FILTER (WHERE status IN ('queued','researching','writing')) AS active_tasks FROM seo_tasks")
        tasks = cursor.fetchone()
        return {"websites": website_count, "tasks": tasks["tasks"], "waiting_for_sources": tasks["waiting_for_sources"], "active_tasks": tasks["active_tasks"]}
