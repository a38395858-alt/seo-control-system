"""Turn collected competitor pages into typed, traceable learning cards.

The service deliberately owns validation and persistence.  Model output is
treated as an untrusted proposal: every source must resolve inside the current
project and competitor observations can never become brand/fact memories.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from typing import Any


CARD_MEMORY_TYPES = {
    "writing_style": "style",
    "content_structure": "editorial",
    "topic_gap": "editorial",
}


@dataclass(frozen=True)
class CompetitorCardLearningResult:
    created_count: int = 0
    updated_count: int = 0
    rejected_count: int = 0
    memory_ids: tuple[int, ...] = ()


class MemoryLearningService:
    """Validate and persist competitor-derived cards for one project."""

    def __init__(self, connection: sqlite3.Connection, *, project_id: int) -> None:
        if not isinstance(project_id, int) or isinstance(project_id, bool) or project_id <= 0:
            raise ValueError("memory learning requires a positive project_id")
        self.connection = connection
        self.project_id = project_id
        if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise ValueError("memory learning project does not exist")

    def save_competitor_cards(
        self,
        cards: Sequence[Mapping[str, Any]],
        *,
        source_documents: Sequence[Mapping[str, Any]],
        learning_run_id: int,
        max_cards: int = 9,
    ) -> CompetitorCardLearningResult:
        """Persist valid cards and bind each one to immutable source versions."""

        sources = self._resolve_sources(source_documents)
        created = updated = rejected = 0
        memory_ids: list[int] = []
        for raw_card in cards[:max_cards]:
            prepared = self._prepare_card(raw_card, sources)
            if prepared is None:
                rejected += 1
                continue
            card_type, topic, summary, used_sources, quality, confidence, applicability = prepared
            source_identity = sorted(str(source["source_id"]) for source in used_sources)
            signature = hashlib.sha256(json.dumps(
                {"card_type": card_type, "topic": topic.casefold(), "source_ids": source_identity},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")).hexdigest()
            source_content_hash = f"competitor-card-v2:{signature}"
            existing = self.connection.execute(
                "SELECT id FROM content_learning_memories WHERE project_id=? AND source_content_hash=? ORDER BY id DESC LIMIT 1",
                (self.project_id, source_content_hash),
            ).fetchone()
            if existing is None and card_type == "writing_style":
                legacy_signature = hashlib.sha256(json.dumps(
                    {"topic": topic.casefold(), "urls": sorted(str(source["url"]) for source in used_sources)},
                    ensure_ascii=False,
                ).encode("utf-8")).hexdigest()
                existing = self.connection.execute(
                    """SELECT id FROM content_learning_memories
                       WHERE project_id=? AND source_content_hash=? ORDER BY id DESC LIMIT 1""",
                    (self.project_id, f"collected-competitor:{legacy_signature}"),
                ).fetchone()
            public_sources = [self._public_source(source) for source in used_sources]
            evidence = {
                "source": "collected_competitor_content_library",
                "learning_run_id": learning_run_id,
                "sources": public_sources,
                "source_count": len(public_sources),
                "policy": "structure_and_method_only_no_competitor_prose",
            }
            values = (
                CARD_MEMORY_TYPES[card_type], card_type, topic, summary,
                json.dumps(evidence, ensure_ascii=False),
                str(used_sources[0]["url"])[:2000], source_content_hash,
                quality, confidence, json.dumps(applicability, ensure_ascii=False),
                len(used_sources),
            )
            if existing is None:
                cursor = self.connection.execute(
                    """INSERT INTO content_learning_memories(
                           project_id,memory_type,card_type,topic,summary,evidence_json,source_url,
                           source_content_hash,quality_score,confidence_score,applicability_json,
                           evidence_count,status,freshness_status,inference_level,last_validated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'active','current','synthesized',CURRENT_TIMESTAMP)""",
                    (self.project_id, *values),
                )
                memory_id = int(cursor.lastrowid)
                created += 1
            else:
                memory_id = int(existing["id"] if isinstance(existing, sqlite3.Row) else existing[0])
                self.connection.execute(
                    """UPDATE content_learning_memories
                       SET memory_type=?,card_type=?,topic=?,summary=?,evidence_json=?,source_url=?,
                           source_content_hash=?,quality_score=?,confidence_score=?,applicability_json=?,
                           evidence_count=?,status='active',freshness_status='current',inference_level='synthesized',
                           last_validated_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND project_id=?""",
                    (*values, memory_id, self.project_id),
                )
                updated += 1
            for source in used_sources:
                self.connection.execute(
                    """INSERT OR IGNORE INTO content_learning_memory_sources(
                           project_id,memory_id,source_type,source_id,source_url,source_content_hash,
                           source_version_id,evidence_excerpt,captured_at
                       ) VALUES(?,?,'competitor_content',?,?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP))""",
                    (
                        self.project_id, memory_id, str(source["source_id"]), str(source["url"])[:2000],
                        str(source["content_hash"])[:128], source.get("version_id"),
                        str(source.get("evidence_excerpt") or "")[:500], source.get("captured_at"),
                    ),
                )
            evidence_count = self.connection.execute(
                "SELECT COUNT(*) FROM content_learning_memory_sources WHERE project_id=? AND memory_id=?",
                (self.project_id, memory_id),
            ).fetchone()[0]
            self.connection.execute(
                "UPDATE content_learning_memories SET evidence_count=? WHERE id=? AND project_id=?",
                (int(evidence_count), memory_id, self.project_id),
            )
            memory_ids.append(memory_id)
        return CompetitorCardLearningResult(created, updated, rejected, tuple(memory_ids))

    def _resolve_sources(self, documents: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        resolved: dict[str, dict[str, Any]] = {}
        for document in documents:
            source_id = str(document.get("source_id") or "")
            match = re.fullmatch(r"competitor-memory-(\d+)", source_id)
            if match is None:
                continue
            memory_id = int(match.group(1))
            row = self.connection.execute(
                """SELECT memory.id,memory.url,memory.domain,memory.page_title,memory.content_hash,
                          versions.id AS version_id,versions.captured_at
                   FROM competitor_content_memory memory
                   LEFT JOIN competitor_content_versions versions
                     ON versions.id=(SELECT latest.id FROM competitor_content_versions latest
                                     WHERE latest.project_id=memory.project_id AND latest.memory_id=memory.id
                                     ORDER BY latest.captured_at DESC,latest.id DESC LIMIT 1)
                   WHERE memory.id=? AND memory.project_id=? AND trim(memory.content)<>''""",
                (memory_id, self.project_id),
            ).fetchone()
            if row is None:
                continue
            value = dict(row)
            value["source_id"] = source_id
            value["evidence_excerpt"] = str(value.get("page_title") or "")
            resolved[source_id] = value
        return resolved

    @staticmethod
    def _prepare_card(
        card: Mapping[str, Any],
        sources: Mapping[str, Mapping[str, Any]],
    ) -> tuple[str, str, str, list[Mapping[str, Any]], float, float, dict[str, list[str]]] | None:
        card_type = str(card.get("card_type") or "writing_style").strip()
        if card_type not in CARD_MEMORY_TYPES:
            return None
        topic = " ".join(str(card.get("topic") or "").split())[:300]
        summary = " ".join(str(card.get("summary") or "").split())[:4000]
        raw_source_ids = card.get("source_ids")
        if not topic or not summary or not isinstance(raw_source_ids, list):
            return None
        used_sources: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for source_id in raw_source_ids:
            key = str(source_id)
            if key in seen or key not in sources:
                continue
            seen.add(key)
            used_sources.append(sources[key])
        if not used_sources:
            return None
        quality = MemoryLearningService._score(card.get("quality_score"), default=0.72, minimum=0.35, maximum=0.92)
        confidence = MemoryLearningService._score(card.get("confidence_score"), default=quality, minimum=0.2, maximum=0.95)
        applicability: dict[str, list[str]] = {}
        raw_applicability = card.get("applicability")
        if isinstance(raw_applicability, Mapping):
            for key in ("search_intents", "content_types", "topics"):
                values = raw_applicability.get(key)
                if isinstance(values, list):
                    applicability[key] = list(dict.fromkeys(
                        " ".join(str(value).split())[:120] for value in values if str(value).strip()
                    ))[:12]
        return card_type, topic, summary, used_sources, quality, confidence, applicability

    @staticmethod
    def _score(value: Any, *, default: float, minimum: float, maximum: float) -> float:
        score = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
        return max(minimum, min(maximum, score))

    @staticmethod
    def _public_source(source: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "source_id": source["source_id"],
            "content_memory_id": source["id"],
            "version_id": source.get("version_id"),
            "title": source.get("page_title") or "",
            "url": source.get("url") or "",
            "domain": source.get("domain") or "",
            "content_hash": source.get("content_hash") or "",
            "captured_at": source.get("captured_at"),
        }
