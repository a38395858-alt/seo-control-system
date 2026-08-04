"""Evidence-gated GSC feedback capture and memory governance.

GSC observations describe what happened after publication.  They are never
treated as proof that a writing technique caused the result.  This service
keeps capture and memory governance separate so a durable Agent job can retry
from the last completed node without creating a misleading extra snapshot.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any
from urllib.parse import urlsplit


class GscFeedbackService:
    """Capture published-page observations and govern performance memories."""

    def __init__(self, connection: sqlite3.Connection, *, project_id: int) -> None:
        if not isinstance(project_id, int) or isinstance(project_id, bool) or project_id <= 0:
            raise ValueError("GSC feedback learning requires a positive project_id")
        self.connection = connection
        self.project_id = project_id
        if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise ValueError("GSC feedback learning project does not exist")

    @staticmethod
    def normalise_page_url(value: str) -> str:
        parsed = urlsplit(value.strip())
        if not parsed.scheme or not parsed.netloc:
            return value.strip().rstrip("/")
        path = parsed.path.rstrip("/") or "/"
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"

    def capture_performance(self, *, window_days: int = 7) -> dict[str, Any]:
        """Persist one snapshot per currently published project article."""

        window_days = max(7, min(365, int(window_days)))
        publications = self.connection.execute(
            """SELECT publications.*,assets.title_snapshot,keywords.keyword,assets.current_draft_id
               FROM content_wordpress_publications publications
               JOIN content_assets assets ON assets.id=publications.content_asset_id
               JOIN keywords ON keywords.id=assets.keyword_id AND keywords.project_id=assets.project_id
               WHERE publications.project_id=? AND publications.status='publish'
                 AND publications.wordpress_url IS NOT NULL AND assets.deleted_at IS NULL
               ORDER BY publications.id DESC""",
            (self.project_id,),
        ).fetchall()
        latest_by_asset: dict[int, sqlite3.Row] = {}
        for publication in publications:
            latest_by_asset.setdefault(int(publication["content_asset_id"]), publication)

        gsc_rows = self.connection.execute(
            """SELECT query,page_url,clicks,impressions,ctr,position
               FROM project_gsc_query_rows WHERE project_id=?""",
            (self.project_id,),
        ).fetchall()
        rows_by_url: dict[str, list[sqlite3.Row]] = {}
        for row in gsc_rows:
            rows_by_url.setdefault(self.normalise_page_url(str(row["page_url"])), []).append(row)

        snapshots: list[dict[str, Any]] = []
        for asset_id, publication in latest_by_asset.items():
            published_url = str(publication["wordpress_url"])
            page_rows = rows_by_url.get(self.normalise_page_url(published_url), [])
            clicks = sum(float(row["clicks"] or 0) for row in page_rows)
            impressions = sum(float(row["impressions"] or 0) for row in page_rows)
            ctr = clicks / impressions if impressions else 0.0
            weighted_position = (
                sum(float(row["position"] or 0) * float(row["impressions"] or 0) for row in page_rows) / impressions
                if impressions else 0.0
            )
            previous = self.connection.execute(
                """SELECT * FROM content_gsc_performance_snapshots
                   WHERE project_id=? AND content_asset_id=?
                   ORDER BY collected_at DESC,id DESC LIMIT 1""",
                (self.project_id, asset_id),
            ).fetchone()
            days_since_previous = None
            if previous is not None:
                days_since_previous = self.connection.execute(
                    "SELECT CAST(julianday('now') - julianday(?) AS INTEGER)",
                    (previous["collected_at"],),
                ).fetchone()[0]
            qualified = previous is not None and (days_since_previous or 0) >= 7 and impressions >= 100 and len(page_rows) >= 3
            learning_status = "qualified" if qualified else ("observing" if previous is None else "insufficient")
            summary = self._snapshot_summary(
                title=str(publication["title_snapshot"]), query_count=len(page_rows), clicks=clicks,
                impressions=impressions, ctr=ctr, position=weighted_position, previous=previous,
            )
            cursor = self.connection.execute(
                """INSERT INTO content_gsc_performance_snapshots(
                       project_id,content_asset_id,draft_id,published_url,window_days,clicks,
                       impressions,ctr,average_position,query_count,learning_status,summary
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.project_id, asset_id, publication["current_draft_id"], published_url, window_days,
                 clicks, impressions, ctr, weighted_position, len(page_rows), learning_status, summary),
            )
            snapshot_id = int(cursor.lastrowid)
            for row in page_rows:
                self.connection.execute(
                    """INSERT INTO content_gsc_performance_rows(
                           snapshot_id,query,page_url,clicks,impressions,ctr,position
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (snapshot_id, row["query"], row["page_url"], row["clicks"], row["impressions"], row["ctr"], row["position"]),
                )
            snapshots.append({
                "content_asset_id": asset_id,
                "snapshot_id": snapshot_id,
                "title": publication["title_snapshot"],
                "published_url": published_url,
                "query_count": len(page_rows),
                "impressions": round(impressions, 2),
                "learning_status": learning_status,
                "memory_id": None,
                "summary": summary,
            })
        return {"snapshots": snapshots, "captured_count": len(snapshots)}

    def update_memory_governance(self, *, snapshot_ids: list[int]) -> dict[str, Any]:
        """Create/update memories only from qualified snapshots with traceable evidence."""

        memory_ids: list[int] = []
        snapshot_memory_ids: dict[int, int] = {}
        for snapshot_id in list(dict.fromkeys(snapshot_ids))[:500]:
            if not isinstance(snapshot_id, int) or isinstance(snapshot_id, bool) or snapshot_id <= 0:
                raise ValueError("snapshot_ids must contain positive integers")
            snapshot = self.connection.execute(
                """SELECT snapshots.*,assets.title_snapshot,keywords.keyword
                   FROM content_gsc_performance_snapshots snapshots
                   JOIN content_assets assets ON assets.id=snapshots.content_asset_id
                   JOIN keywords ON keywords.id=assets.keyword_id AND keywords.project_id=assets.project_id
                   WHERE snapshots.id=? AND snapshots.project_id=? AND assets.deleted_at IS NULL""",
                (snapshot_id, self.project_id),
            ).fetchone()
            if snapshot is None:
                raise ValueError("GSC snapshot does not exist in this project")
            if str(snapshot["learning_status"]) != "qualified":
                continue
            memory_id = self._upsert_performance_memory(snapshot)
            memory_ids.append(memory_id)
            snapshot_memory_ids[snapshot_id] = memory_id

        self.connection.execute(
            """UPDATE content_learning_memories
               SET freshness_status=CASE
                     WHEN julianday('now')-julianday(COALESCE(last_validated_at,updated_at)) > 180 THEN 'stale'
                     WHEN julianday('now')-julianday(COALESCE(last_validated_at,updated_at)) > 90 THEN 'needs_review'
                     ELSE freshness_status END,
                   updated_at=updated_at
               WHERE project_id=? AND memory_type='performance' AND status='active'""",
            (self.project_id,),
        )
        return {
            "memories_created": len(memory_ids),
            "memory_ids": memory_ids,
            "snapshot_memory_ids": {str(key): value for key, value in snapshot_memory_ids.items()},
        }

    def _upsert_performance_memory(self, snapshot: sqlite3.Row) -> int:
        asset_id = int(snapshot["content_asset_id"])
        snapshot_id = int(snapshot["id"])
        published_url = str(snapshot["published_url"])
        source_hash = f"gsc-performance:{self.project_id}:{asset_id}:{self.normalise_page_url(published_url)}"
        evidence = {
            "source": "Google Search Console",
            "snapshot_id": snapshot_id,
            "content_asset_id": asset_id,
            "published_url": published_url,
            "window_days": int(snapshot["window_days"]),
            "clicks": float(snapshot["clicks"] or 0),
            "impressions": float(snapshot["impressions"] or 0),
            "ctr": float(snapshot["ctr"] or 0),
            "average_position": float(snapshot["average_position"] or 0),
            "query_count": int(snapshot["query_count"] or 0),
            "collected_at": snapshot["collected_at"],
            "causality": "descriptive_only",
        }
        impressions = float(snapshot["impressions"] or 0)
        query_count = int(snapshot["query_count"] or 0)
        quality_score = 0.7 if impressions >= 500 else 0.55
        confidence_score = min(0.9, 0.55 + min(impressions, 1000) / 5000 + min(query_count, 10) / 100)
        applicability = {
            "topics": [str(snapshot["keyword"] or snapshot["title_snapshot"])[:120]],
            "content_types": ["published_seo_article"],
            "scope": "same project; descriptive post-publication feedback only",
        }
        existing = self.connection.execute(
            "SELECT id FROM content_learning_memories WHERE project_id=? AND source_content_hash=?",
            (self.project_id, source_hash),
        ).fetchone()
        values = (
            str(snapshot["keyword"] or snapshot["title_snapshot"])[:300], str(snapshot["summary"])[:12000],
            json.dumps(evidence, ensure_ascii=False), published_url[:2000], quality_score, confidence_score,
            json.dumps(applicability, ensure_ascii=False),
        )
        if existing is None:
            cursor = self.connection.execute(
                """INSERT INTO content_learning_memories(
                       project_id,memory_type,card_type,topic,summary,evidence_json,source_url,
                       source_content_hash,quality_score,confidence_score,applicability_json,
                       status,freshness_status,inference_level,last_validated_at
                   ) VALUES(?,'performance','performance',?,?,?,?,?,?,?,?,'active','current','observed',CURRENT_TIMESTAMP)""",
                (self.project_id, *values[:4], source_hash, *values[4:]),
            )
            memory_id = int(cursor.lastrowid)
        else:
            memory_id = int(existing["id"] if isinstance(existing, sqlite3.Row) else existing[0])
            self.connection.execute(
                """UPDATE content_learning_memories
                   SET card_type='performance',topic=?,summary=?,evidence_json=?,source_url=?,
                       quality_score=?,confidence_score=?,applicability_json=?,status='active',
                       freshness_status='current',inference_level='observed',last_validated_at=CURRENT_TIMESTAMP,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND project_id=?""",
                (*values, memory_id, self.project_id),
            )

        snapshot_hash = hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        self.connection.execute(
            """INSERT OR IGNORE INTO content_learning_memory_sources(
                   project_id,memory_id,source_type,source_id,source_url,source_content_hash,
                   source_version_id,evidence_excerpt,captured_at
               ) VALUES(?,?,'gsc',?,?,?,NULL,?,?)""",
            (self.project_id, memory_id, str(snapshot_id), published_url[:2000], snapshot_hash,
             str(snapshot["summary"])[:500], snapshot["collected_at"]),
        )
        evidence_count = int(self.connection.execute(
            "SELECT COUNT(*) FROM content_learning_memory_sources WHERE project_id=? AND memory_id=?",
            (self.project_id, memory_id),
        ).fetchone()[0])
        self.connection.execute(
            "UPDATE content_learning_memories SET evidence_count=? WHERE id=? AND project_id=?",
            (evidence_count, memory_id, self.project_id),
        )
        return memory_id

    @staticmethod
    def _snapshot_summary(
        *, title: str, query_count: int, clicks: float, impressions: float,
        ctr: float, position: float, previous: sqlite3.Row | None,
    ) -> str:
        base = (
            f"《{title}》本次匹配到 {query_count} 个 GSC 查询，获得 {clicks:.0f} 次点击、"
            f"{impressions:.0f} 次展示，CTR {ctr * 100:.1f}%，平均排名 {position:.1f}。"
        )
        if previous is None:
            return base + "这是首个观察快照，不作为写作因果结论。"
        movement = impressions - float(previous["impressions"] or 0)
        return base + f"与上一次快照相比，展示变化 {movement:+.0f}。该数据只描述搜索表现，不证明某种写法单独造成变化。"
