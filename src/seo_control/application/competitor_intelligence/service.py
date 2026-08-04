"""Durable, project-scoped primitives for the unified collection pipeline.

The service owns persistence and idempotency. Network access remains in the
existing crawler adapters, so Agent code never receives a database connection
or an unrestricted crawler client.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_PARAMETERS = frozenset({
    "dclid", "fbclid", "gclid", "gbraid", "mc_cid", "mc_eid", "msclkid",
    "rsltid", "srsltid", "ttclid", "twclid", "wbraid",
})


def normalize_collection_url(value: str) -> str:
    """Return one stable key for a public page without ad tracking noise."""
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return raw.split("#", 1)[0].rstrip("/").casefold()
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in TRACKING_QUERY_PARAMETERS
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((
        parsed.scheme.casefold(), parsed.netloc.casefold(), path,
        urlencode(sorted(query)), "",
    ))


@dataclass(frozen=True)
class ContentWriteResult:
    memory_id: int
    version_id: int
    outcome: str  # collected | unchanged
    content_hash: str


class CollectionService:
    """Persistence boundary for plans, run items and content versions."""

    PLAN_SOURCE_TYPES = frozenset({"domain", "keyword", "first_party"})
    TERMINAL_ITEM_STATUSES = frozenset({
        "collected", "unchanged", "robots_blocked", "excluded", "failed",
    })

    @staticmethod
    def assert_project(connection: sqlite3.Connection, project_id: int) -> None:
        if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise ValueError("project does not exist")

    def save_plan(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: int,
        source_type: str,
        source_value: str,
        enabled: bool = True,
        schedule: str = "manual",
        settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.assert_project(connection, project_id)
        if source_type not in self.PLAN_SOURCE_TYPES:
            raise ValueError("source_type must be domain, keyword, or first_party")
        source_value = " ".join(source_value.split())
        if not source_value:
            raise ValueError("source_value is required")
        settings_json = json.dumps(dict(settings or {}), ensure_ascii=False)
        connection.execute(
            """INSERT INTO collection_plans(
                   project_id,source_type,source_value,status,schedule,settings_json
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(project_id,source_type,source_value) DO UPDATE SET
                   status=excluded.status,schedule=excluded.schedule,
                   settings_json=excluded.settings_json,updated_at=CURRENT_TIMESTAMP""",
            (project_id, source_type, source_value, "active" if enabled else "paused", schedule[:80], settings_json),
        )
        row = connection.execute(
            "SELECT * FROM collection_plans WHERE project_id=? AND source_type=? AND source_value=?",
            (project_id, source_type, source_value),
        ).fetchone()
        return self._plan_payload(row)

    def list_plans(self, connection: sqlite3.Connection, *, project_id: int) -> list[dict[str, Any]]:
        self.assert_project(connection, project_id)
        rows = connection.execute(
            "SELECT * FROM collection_plans WHERE project_id=? ORDER BY updated_at DESC,id DESC",
            (project_id,),
        ).fetchall()
        return [self._plan_payload(row) for row in rows]

    def get_plan(self, connection: sqlite3.Connection, *, project_id: int, plan_id: int) -> dict[str, Any]:
        self.assert_project(connection, project_id)
        row = connection.execute(
            "SELECT * FROM collection_plans WHERE id=? AND project_id=?",
            (plan_id, project_id),
        ).fetchone()
        if row is None:
            raise ValueError("collection plan does not exist in this project")
        return self._plan_payload(row)

    @staticmethod
    def _plan_payload(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        try:
            settings = json.loads(value.pop("settings_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            settings = {}
        value["settings"] = settings if isinstance(settings, dict) else {}
        return value

    def create_catalog_run(self, connection: sqlite3.Connection, *, project_id: int) -> tuple[dict[str, Any], list[int]]:
        """Create a compatible batch plus durable per-URL execution items."""
        self.assert_project(connection, project_id)
        active = connection.execute(
            "SELECT 1 FROM competitor_catalog_collection_runs WHERE project_id=? AND status IN ('queued','running')",
            (project_id,),
        ).fetchone()
        if active is not None:
            raise ValueError("a bulk competitor collection run is already queued or running for this project")

        memory_rows = connection.execute(
            "SELECT id,url,normalized_url FROM competitor_content_memory WHERE project_id=?",
            (project_id,),
        ).fetchall()
        existing_memory = {
            normalize_collection_url(str(row["normalized_url"] or row["url"])): int(row["id"])
            for row in memory_rows
        }
        catalog_rows = connection.execute(
            """SELECT id,url,normalized_url,domain,search_title,last_rank,last_query,collection_status
               FROM competitor_url_catalog WHERE project_id=?
               AND collection_status NOT IN ('excluded','robots_blocked')
               ORDER BY COALESCE(last_rank,999999),id""",
            (project_id,),
        ).fetchall()
        candidates: list[sqlite3.Row] = []
        seen: set[str] = set()
        already_collected = 0
        for row in catalog_rows:
            canonical = normalize_collection_url(str(row["url"]))
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            memory_id = existing_memory.get(canonical)
            if memory_id is not None:
                self.mark_catalog(connection, project_id=project_id, normalized_url=canonical, status="collected", memory_id=memory_id)
                already_collected += 1
                continue
            candidates.append(row)

        candidate_ids = [int(row["id"]) for row in candidates]
        status = "queued" if candidates else "completed"
        cursor = connection.execute(
            """INSERT INTO competitor_catalog_collection_runs(
                   project_id,status,candidate_ids_json,total_count,already_collected_count,completed_at,last_heartbeat_at
               ) VALUES(?,?,?,?,?,CASE WHEN ?='completed' THEN CURRENT_TIMESTAMP ELSE NULL END,CURRENT_TIMESTAMP)""",
            (project_id, status, json.dumps(candidate_ids), len(candidates), already_collected, status),
        )
        run_id = int(cursor.lastrowid)
        for row in candidates:
            canonical = normalize_collection_url(str(row["url"]))
            connection.execute(
                """INSERT INTO collection_run_items(
                       project_id,run_id,catalog_id,normalized_url,source_url,status
                   ) VALUES(?,?,?,?,?,'queued')""",
                (project_id, run_id, int(row["id"]), canonical, str(row["url"])),
            )
            self.mark_catalog(connection, project_id=project_id, normalized_url=canonical, status="queued")
        row = connection.execute("SELECT * FROM competitor_catalog_collection_runs WHERE id=?", (run_id,)).fetchone()
        return dict(row), candidate_ids

    def register_catalog_result(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: int,
        result: Mapping[str, Any],
        collection_status: str,
        reason: str = "",
        memory_id: int | None = None,
    ) -> int | None:
        """Idempotently add one discovered URL to the project catalog."""
        self.assert_project(connection, project_id)
        url = str(result.get("url") or "").strip()
        normalized = normalize_collection_url(url)
        if not normalized:
            return None
        connection.execute(
            """INSERT INTO competitor_url_catalog(
                   project_id,normalized_url,url,domain,search_title,collection_status,
                   exclusion_reason,last_rank,last_query,memory_id,last_collected_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,CASE WHEN ?='collected' THEN CAST(CURRENT_TIMESTAMP AS TEXT) ELSE NULL END)
               ON CONFLICT(project_id,normalized_url) DO UPDATE SET
                   url=excluded.url,domain=excluded.domain,search_title=excluded.search_title,
                   collection_status=CASE
                     WHEN competitor_url_catalog.collection_status='collected' AND excluded.collection_status!='collected'
                     THEN 'collected' ELSE excluded.collection_status END,
                   exclusion_reason=CASE
                     WHEN competitor_url_catalog.collection_status='collected' AND excluded.collection_status!='collected'
                     THEN competitor_url_catalog.exclusion_reason ELSE excluded.exclusion_reason END,
                   last_rank=excluded.last_rank,last_query=excluded.last_query,
                   memory_id=COALESCE(excluded.memory_id,competitor_url_catalog.memory_id),
                   discovered_count=competitor_url_catalog.discovered_count+1,
                   last_seen_at=CURRENT_TIMESTAMP,
                   last_collected_at=CASE WHEN excluded.collection_status='collected'
                     THEN CAST(CURRENT_TIMESTAMP AS TEXT) ELSE competitor_url_catalog.last_collected_at END""",
            (project_id, normalized, url, str(result.get("domain") or ""), str(result.get("title") or ""),
             collection_status, reason[:1200], int(result.get("rank") or 0) or None,
             str(result.get("search_query") or ""), memory_id, collection_status),
        )
        row = connection.execute(
            "SELECT id FROM competitor_url_catalog WHERE project_id=? AND normalized_url=?",
            (project_id, normalized),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def ensure_legacy_run_items(self, connection: sqlite3.Connection, *, project_id: int, run_id: int) -> None:
        """Materialize execution items for a pre-Migration-39 queued run."""
        existing = connection.execute(
            "SELECT 1 FROM collection_run_items WHERE project_id=? AND run_id=? LIMIT 1",
            (project_id, run_id),
        ).fetchone()
        if existing is not None:
            return
        run = connection.execute(
            "SELECT candidate_ids_json FROM competitor_catalog_collection_runs WHERE id=? AND project_id=?",
            (run_id, project_id),
        ).fetchone()
        if run is None:
            raise ValueError("competitor catalog collection run does not exist in this project")
        try:
            candidate_ids = json.loads(run["candidate_ids_json"] or "[]")
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("competitor catalog collection run has invalid candidates") from error
        if not isinstance(candidate_ids, list) or not all(isinstance(item, int) for item in candidate_ids):
            raise ValueError("competitor catalog collection run has invalid candidates")
        for catalog_id in candidate_ids:
            row = connection.execute(
                "SELECT id,url,normalized_url FROM competitor_url_catalog WHERE id=? AND project_id=?",
                (catalog_id, project_id),
            ).fetchone()
            if row is None:
                continue
            canonical = normalize_collection_url(str(row["url"]))
            connection.execute(
                """INSERT OR IGNORE INTO collection_run_items(
                       project_id,run_id,catalog_id,normalized_url,source_url,status
                   ) VALUES(?,?,?,?,?,'queued')""",
                (project_id, run_id, int(row["id"]), canonical, str(row["url"])),
            )

    def claim_run_items(self, connection: sqlite3.Connection, *, project_id: int, run_id: int) -> list[dict[str, Any]]:
        self.assert_project(connection, project_id)
        run = connection.execute(
            "SELECT * FROM competitor_catalog_collection_runs WHERE id=? AND project_id=?",
            (run_id, project_id),
        ).fetchone()
        if run is None:
            raise ValueError("competitor catalog collection run does not exist in this project")
        if str(run["status"]) not in {"queued", "running"}:
            return []
        self.ensure_legacy_run_items(connection, project_id=project_id, run_id=run_id)
        rows = connection.execute(
            """SELECT items.*,catalog.domain,catalog.search_title,catalog.last_rank,catalog.last_query
               FROM collection_run_items items
               JOIN competitor_url_catalog catalog
                 ON catalog.id=items.catalog_id AND catalog.project_id=items.project_id
               WHERE items.project_id=? AND items.run_id=? AND items.status='queued'
               ORDER BY COALESCE(catalog.last_rank,999999),items.id""",
            (project_id, run_id),
        ).fetchall()
        if not rows:
            self.recalculate_run(connection, project_id=project_id, run_id=run_id, complete_if_idle=True)
            return []
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        connection.execute(
            f"""UPDATE collection_run_items SET status='running',attempt_count=attempt_count+1,
                    started_at=COALESCE(started_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP
                WHERE project_id=? AND id IN ({placeholders}) AND status='queued'""",
            (project_id, *ids),
        )
        connection.execute(
            """UPDATE competitor_catalog_collection_runs SET status='running',
                   started_at=COALESCE(started_at,CURRENT_TIMESTAMP),last_heartbeat_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
            (run_id, project_id),
        )
        return [dict(row) for row in rows]

    def persist_content(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: int,
        result: Mapping[str, Any],
        page: Mapping[str, Any],
        chunks: Callable[[str], Sequence[str]],
        extractor: str = "competitor_content_client",
    ) -> ContentWriteResult:
        self.assert_project(connection, project_id)
        url = str(result.get("url") or "").strip()
        normalized = normalize_collection_url(url)
        content = str(page.get("content") or "").strip()
        if not normalized or not content:
            raise ValueError("competitor extraction returned no article content")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        headings = [line for line in content.splitlines() if len(line) < 160][:24]
        structure_json = json.dumps({"sample_lines": headings}, ensure_ascii=False)
        existing = connection.execute(
            "SELECT id,content_hash FROM competitor_content_memory WHERE project_id=? AND normalized_url=?",
            (project_id, normalized),
        ).fetchone()
        outcome = "unchanged" if existing is not None and str(existing["content_hash"]) == digest else "collected"
        connection.execute(
            """INSERT INTO competitor_content_memory(
                   project_id,normalized_url,url,domain,page_title,content,content_hash,structure_json
               ) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(project_id,normalized_url) DO UPDATE SET
                   url=excluded.url,domain=excluded.domain,page_title=excluded.page_title,
                   content=excluded.content,content_hash=excluded.content_hash,
                   structure_json=excluded.structure_json,last_captured_at=CURRENT_TIMESTAMP""",
            (project_id, normalized, url, str(page.get("domain") or result.get("domain") or ""),
             str(page.get("title") or result.get("title") or ""), content, digest, structure_json),
        )
        memory = connection.execute(
            "SELECT id FROM competitor_content_memory WHERE project_id=? AND normalized_url=?",
            (project_id, normalized),
        ).fetchone()
        memory_id = int(memory["id"])
        connection.execute(
            """INSERT OR IGNORE INTO competitor_content_versions(
                   project_id,memory_id,content_hash,content,structure_json,extractor
               ) VALUES(?,?,?,?,?,?)""",
            (project_id, memory_id, digest, content, structure_json, extractor[:120]),
        )
        version = connection.execute(
            "SELECT id FROM competitor_content_versions WHERE project_id=? AND memory_id=? AND content_hash=?",
            (project_id, memory_id, digest),
        ).fetchone()
        version_id = int(version["id"])
        if outcome == "collected":
            connection.execute("DELETE FROM competitor_content_chunks WHERE memory_id=? AND project_id=?", (memory_id, project_id))
            for position, chunk in enumerate(chunks(content), 1):
                connection.execute(
                    "INSERT INTO competitor_content_chunks(project_id,memory_id,position,content) VALUES(?,?,?,?)",
                    (project_id, memory_id, position, chunk),
                )
        return ContentWriteResult(memory_id, version_id, outcome, digest)

    def finish_item(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: int,
        run_id: int,
        item_id: int,
        status: str,
        memory_id: int | None = None,
        version_id: int | None = None,
        extractor: str = "",
        error_summary: str = "",
    ) -> None:
        if status not in self.TERMINAL_ITEM_STATUSES:
            raise ValueError("invalid terminal collection item status")
        cursor = connection.execute(
            """UPDATE collection_run_items SET status=?,memory_id=?,version_id=?,extractor=?,error_summary=?,
                   completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND run_id=? AND project_id=?""",
            (status, memory_id, version_id, extractor[:120], error_summary[:1200], item_id, run_id, project_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("collection run item does not exist in this project")
        self.recalculate_run(connection, project_id=project_id, run_id=run_id)

    def recalculate_run(self, connection: sqlite3.Connection, *, project_id: int, run_id: int, complete_if_idle: bool = False) -> dict[str, Any]:
        run = connection.execute(
            "SELECT * FROM competitor_catalog_collection_runs WHERE id=? AND project_id=?",
            (run_id, project_id),
        ).fetchone()
        if run is None:
            raise ValueError("competitor catalog collection run does not exist in this project")
        counts = {
            str(row["status"]): int(row["amount"])
            for row in connection.execute(
                "SELECT status,COUNT(*) AS amount FROM collection_run_items WHERE project_id=? AND run_id=? GROUP BY status",
                (project_id, run_id),
            ).fetchall()
        }
        idle = not counts.get("queued", 0) and not counts.get("running", 0)
        status = "completed" if idle and (counts or complete_if_idle) else str(run["status"])
        connection.execute(
            """UPDATE competitor_catalog_collection_runs SET status=?,collected_count=?,unchanged_count=?,
                   robots_blocked_count=?,failed_count=?,last_heartbeat_at=CURRENT_TIMESTAMP,
                   completed_at=CASE WHEN ?='completed' THEN COALESCE(completed_at,CURRENT_TIMESTAMP) ELSE completed_at END,
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
            (status, counts.get("collected", 0), counts.get("unchanged", 0), counts.get("robots_blocked", 0),
             counts.get("failed", 0), status, run_id, project_id),
        )
        row = connection.execute(
            "SELECT * FROM competitor_catalog_collection_runs WHERE id=? AND project_id=?",
            (run_id, project_id),
        ).fetchone()
        return dict(row)

    def list_run_items(self, connection: sqlite3.Connection, *, project_id: int, run_id: int) -> list[dict[str, Any]]:
        self.assert_project(connection, project_id)
        if connection.execute(
            "SELECT 1 FROM competitor_catalog_collection_runs WHERE id=? AND project_id=?",
            (run_id, project_id),
        ).fetchone() is None:
            raise ValueError("competitor catalog collection run does not exist in this project")
        rows = connection.execute(
            """SELECT items.*,catalog.domain,catalog.search_title,catalog.last_rank,catalog.last_query
               FROM collection_run_items items
               LEFT JOIN competitor_url_catalog catalog
                 ON catalog.id=items.catalog_id AND catalog.project_id=items.project_id
               WHERE items.project_id=? AND items.run_id=? ORDER BY items.id""",
            (project_id, run_id),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def mark_catalog(
        connection: sqlite3.Connection,
        *,
        project_id: int,
        normalized_url: str,
        status: str,
        memory_id: int | None = None,
        reason: str = "",
    ) -> None:
        rows = connection.execute(
            "SELECT id,url,normalized_url FROM competitor_url_catalog WHERE project_id=?",
            (project_id,),
        ).fetchall()
        matching_ids = [
            int(row["id"])
            for row in rows
            if normalize_collection_url(str(row["url"])) == normalized_url
        ]
        if not matching_ids:
            return
        placeholders = ",".join("?" for _ in matching_ids)
        if status == "collected":
            connection.execute(
                f"""UPDATE competitor_url_catalog SET collection_status='collected',exclusion_reason='',
                       memory_id=?,last_collected_at=CURRENT_TIMESTAMP,last_seen_at=CURRENT_TIMESTAMP
                   WHERE project_id=? AND id IN ({placeholders})""",
                (memory_id, project_id, *matching_ids),
            )
        else:
            connection.execute(
                f"""UPDATE competitor_url_catalog SET collection_status=?,exclusion_reason=?,last_seen_at=CURRENT_TIMESTAMP
                   WHERE project_id=? AND id IN ({placeholders})""",
                (status, reason[:1200], project_id, *matching_ids),
            )

    def recover_interrupted_runs(self, connection: sqlite3.Connection) -> list[tuple[int, int]]:
        """Return queued runs after restoring crash-interrupted URL items."""
        interrupted = connection.execute(
            """SELECT DISTINCT runs.id,runs.project_id
               FROM competitor_catalog_collection_runs runs
               LEFT JOIN collection_run_items items ON items.run_id=runs.id AND items.project_id=runs.project_id
               WHERE runs.status IN ('queued','running')
                 AND (items.status IN ('queued','running') OR items.id IS NULL)
               ORDER BY runs.id"""
        ).fetchall()
        recovered: list[tuple[int, int]] = []
        for run in interrupted:
            run_id, project_id = int(run["id"]), int(run["project_id"])
            running_count = int(connection.execute(
                "SELECT COUNT(*) FROM collection_run_items WHERE run_id=? AND project_id=? AND status='running'",
                (run_id, project_id),
            ).fetchone()[0])
            if running_count:
                connection.execute(
                    """UPDATE collection_run_items SET status='queued',
                           error_summary='Recovered after service restart.',updated_at=CURRENT_TIMESTAMP
                       WHERE run_id=? AND project_id=? AND status='running'""",
                    (run_id, project_id),
                )
                connection.execute(
                    """UPDATE competitor_catalog_collection_runs SET status='queued',
                           recovered_count=recovered_count+?,completed_at=NULL,updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND project_id=?""",
                    (running_count, run_id, project_id),
                )
            elif str(connection.execute(
                "SELECT status FROM competitor_catalog_collection_runs WHERE id=? AND project_id=?",
                (run_id, project_id),
            ).fetchone()[0]) == "running":
                connection.execute(
                    "UPDATE competitor_catalog_collection_runs SET status='queued',completed_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?",
                    (run_id, project_id),
                )
            recovered.append((project_id, run_id))
        return recovered
