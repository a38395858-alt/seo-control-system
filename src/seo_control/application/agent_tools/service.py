"""Server-side Agent tools with strict project scope and durable audit records.

The model and LangGraph never receive a database connection.  They can only
invoke the names in :data:`AGENT_TOOL_ALLOWLIST` through this service.  Every
invocation validates the project again and writes a compact, secret-free audit
record, whether the call succeeds or fails.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
import sqlite3
import time
from typing import Any, Callable


AGENT_TOOL_ALLOWLIST = frozenset({
    "load_project_context",
    "retrieve_project_memories",
    "research_competitors",
    "collect_competitor_content",
    "collect_first_party_sources",
    "extract_learning_cards",
    "generate_content_blueprint",
    "generate_article",
    "review_article",
    "prepare_publish",
    "capture_gsc_performance",
    "update_memory_governance",
})

_SECRET_MARKERS = ("api_key", "password", "secret", "token", "cookie", "authorization")


class AgentToolScopeError(ValueError):
    """Raised when a tool request does not belong to its workflow project."""


@dataclass(frozen=True)
class AgentToolCallResult:
    tool_name: str
    output: dict[str, Any]
    duration_ms: int


class AgentToolService:
    """Execute the first project-safe tools used by the content workflow."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: int,
        job_id: int | None = None,
        handlers: Mapping[str, Callable[[Mapping[str, Any]], dict[str, Any]]] | None = None,
    ) -> None:
        if not isinstance(project_id, int) or isinstance(project_id, bool) or project_id <= 0:
            raise AgentToolScopeError("Agent tools require a positive project_id")
        self.connection = connection
        self.project_id = project_id
        self.job_id = job_id
        self._handlers: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            "load_project_context": self._load_project_context,
            "retrieve_project_memories": self._retrieve_project_memories,
        }
        if handlers:
            unsupported = sorted(set(handlers) - AGENT_TOOL_ALLOWLIST)
            if unsupported:
                raise ValueError(f"Agent tool handlers are not allowlisted: {', '.join(unsupported)}")
            duplicate = sorted(set(handlers) & self._handlers.keys())
            if duplicate:
                raise ValueError(f"Agent tool handlers cannot replace core tools: {', '.join(duplicate)}")
            self._handlers.update(handlers)
        self._validate_persistent_scope()

    @property
    def implemented_tools(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def invoke(self, tool_name: str, payload: Mapping[str, Any]) -> AgentToolCallResult:
        """Run one allowlisted tool and record a compact audit row."""

        started = time.perf_counter()
        try:
            if tool_name not in AGENT_TOOL_ALLOWLIST:
                raise ValueError(f"Agent tool is not allowlisted: {tool_name}")
            handler = self._handlers.get(tool_name)
            if handler is None:
                raise ValueError(f"Agent tool is planned but not implemented: {tool_name}")
            self._validate_payload_scope(payload)
            output = handler(payload)
        except Exception as error:
            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            self._write_audit(
                tool_name=tool_name,
                status="failed",
                payload=payload,
                output={},
                duration_ms=duration_ms,
                error_summary=self._redact_error(str(error)),
            )
            raise
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        self._write_audit(
            tool_name=tool_name,
            status="completed",
            payload=payload,
            output=self._audit_output(tool_name, output),
            duration_ms=duration_ms,
            error_summary=None,
        )
        return AgentToolCallResult(tool_name=tool_name, output=output, duration_ms=duration_ms)

    def _validate_persistent_scope(self) -> None:
        if self.connection.execute("SELECT 1 FROM projects WHERE id=?", (self.project_id,)).fetchone() is None:
            raise AgentToolScopeError("Agent tool project does not exist")
        if self.job_id is not None and self.connection.execute(
            "SELECT 1 FROM agent_jobs WHERE id=? AND project_id=?",
            (self.job_id, self.project_id),
        ).fetchone() is None:
            raise AgentToolScopeError("Agent job does not exist in this project")

    def _validate_payload_scope(self, payload: Mapping[str, Any]) -> None:
        supplied = payload.get("project_id")
        if not isinstance(supplied, int) or isinstance(supplied, bool) or supplied != self.project_id:
            raise AgentToolScopeError("Agent tool project_id does not match the workflow project")
        self._validate_persistent_scope()

    def _load_project_context(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        project = self.connection.execute(
            """SELECT id,name,site_url,industry,default_country,default_language
               FROM projects WHERE id=?""",
            (self.project_id,),
        ).fetchone()
        if project is None:
            raise AgentToolScopeError("Agent tool project does not exist")
        counts = {
            "keywords": self._count("keywords"),
            "content_assets": self._count("content_assets", "deleted_at IS NULL"),
            "company_knowledge": self._count("project_knowledge_documents"),
            "competitor_articles": self._count("competitor_content_memory"),
            "active_memories": self._count("content_learning_memories", "status='active'"),
            "gsc_rows": self._count("project_gsc_query_rows"),
        }
        result: dict[str, Any] = {"project": dict(project), "counts": counts}
        content_asset_id = payload.get("content_asset_id")
        if content_asset_id is not None:
            if not isinstance(content_asset_id, int) or isinstance(content_asset_id, bool) or content_asset_id <= 0:
                raise ValueError("content_asset_id must be a positive integer")
            asset = self.connection.execute(
                """SELECT assets.id,assets.title_snapshot,assets.locale,assets.country_code,assets.content_type,
                          assets.status,keywords.keyword
                   FROM content_assets assets
                   LEFT JOIN keywords ON keywords.id=assets.keyword_id AND keywords.project_id=assets.project_id
                   WHERE assets.id=? AND assets.project_id=? AND assets.deleted_at IS NULL""",
                (content_asset_id, self.project_id),
            ).fetchone()
            if asset is None:
                raise AgentToolScopeError("content asset does not exist in this project")
            result["content_asset"] = dict(asset)
        return result

    def _retrieve_project_memories(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        query = payload.get("query", "")
        if not isinstance(query, str):
            raise ValueError("memory query must be text")
        limit = payload.get("limit", 7)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("memory limit must be an integer from 1 to 20")
        memory_type = payload.get("memory_type")
        if memory_type is not None and memory_type not in {"style", "brand", "fact", "performance", "editorial"}:
            raise ValueError("unsupported memory_type")
        parameters: list[Any] = [self.project_id]
        condition = "project_id=? AND status='active'"
        if memory_type is not None:
            condition += " AND memory_type=?"
            parameters.append(memory_type)
        rows = self.connection.execute(
            f"""SELECT * FROM content_learning_memories WHERE {condition}
                ORDER BY pinned DESC,manual_priority DESC,quality_score DESC,updated_at DESC,id DESC""",
            tuple(parameters),
        ).fetchall()
        query_terms = self._terms(query)
        ranked: list[tuple[float, sqlite3.Row, list[str]]] = []
        for row in rows:
            applicability = row["applicability_json"] if "applicability_json" in row.keys() else "{}"
            memory_terms = self._terms(f"{row['topic']} {row['summary']} {applicability}")
            matched = sorted(query_terms & memory_terms)
            pinned = bool(row["pinned"])
            if query_terms and not matched:
                continue
            if not query_terms and not pinned:
                continue
            quality = float(row["quality_score"])
            confidence = float(row["confidence_score"] if "confidence_score" in row.keys() else 0.5)
            freshness = str(row["freshness_status"] if "freshness_status" in row.keys() else "current")
            freshness_factor = 1.0 if freshness == "current" else 0.82 if freshness == "needs_review" else 0.68
            score = (quality * 0.45 + confidence * 0.15 + min(len(matched), 5) * 0.08)
            score += int(row["manual_priority"]) * 0.018
            score += 0.08 if pinned else 0
            score += min(int(row["positive_feedback_count"]), 8) * 0.015
            score -= min(int(row["negative_feedback_count"]), 8) * 0.025
            ranked.append((max(0.0, min(1.0, score * freshness_factor)), row, matched))
        selected = sorted(ranked, key=lambda item: (item[0], item[1]["updated_at"], item[1]["id"]), reverse=True)[:limit]
        memories: list[dict[str, Any]] = []
        for score, row, matched in selected:
            confidence = float(row["confidence_score"] if "confidence_score" in row.keys() else 0.5)
            freshness = str(row["freshness_status"] if "freshness_status" in row.keys() else "current")
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                evidence = {}
            reasons = []
            if matched:
                reasons.append(f"Matched topic terms: {', '.join(matched[:8])}")
            if row["pinned"]:
                reasons.append("Pinned by a user")
            if int(row["manual_priority"]):
                reasons.append(f"Manual priority {int(row['manual_priority']):+d}")
            evidence_count = int(row["evidence_count"] if "evidence_count" in row.keys() else 0)
            reasons.append(f"Confidence {round(confidence * 100)}%; {evidence_count} traceable source(s); freshness {freshness}")
            source_rows = self.connection.execute(
                """SELECT source_type,source_id,source_url,source_content_hash,source_version_id,captured_at
                   FROM content_learning_memory_sources WHERE project_id=? AND memory_id=?
                   ORDER BY captured_at DESC,id DESC""",
                (self.project_id, int(row["id"])),
            ).fetchall()
            try:
                applicability_value = json.loads(row["applicability_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                applicability_value = {}
            memories.append({
                "memory_id": int(row["id"]),
                "memory_type": row["memory_type"],
                "card_type": row["card_type"],
                "topic": row["topic"],
                "summary": row["summary"],
                "source_url": row["source_url"],
                "sources": [dict(source) for source in source_rows],
                "evidence": evidence,
                "applicability": applicability_value,
                "quality_score": quality,
                "confidence_score": confidence,
                "freshness_status": freshness,
                "evidence_count": evidence_count,
                "relevance_score": round(score, 3),
                "selection_reason": "; ".join(reasons) or "Active project memory",
            })
        return {"query": query.strip(), "selected_count": len(memories), "memories": memories}

    def _count(self, table: str, extra_condition: str = "") -> int:
        allowed = {
            "keywords", "content_assets", "project_knowledge_documents",
            "competitor_content_memory", "content_learning_memories", "project_gsc_query_rows",
        }
        if table not in allowed:
            raise ValueError("unsupported project context table")
        condition = "project_id=?" + (f" AND {extra_condition}" if extra_condition else "")
        row = self.connection.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {condition}", (self.project_id,)).fetchone()
        return int(row["count"] if isinstance(row, sqlite3.Row) else row[0])

    def _write_audit(
        self,
        *,
        tool_name: str,
        status: str,
        payload: Mapping[str, Any],
        output: Mapping[str, Any],
        duration_ms: int,
        error_summary: str | None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO agent_tool_audits(
                   project_id,job_id,tool_name,status,input_json,output_json,duration_ms,error_summary,completed_at
               ) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (
                self.project_id,
                self.job_id,
                tool_name,
                status,
                json.dumps(self._redact(payload), ensure_ascii=False),
                json.dumps(self._redact(output), ensure_ascii=False),
                duration_ms,
                error_summary,
            ),
        )

    @staticmethod
    def _terms(value: str) -> set[str]:
        return {term.casefold() for term in re.findall(r"[A-Za-z0-9]{3,}", value)}

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): "[redacted]" if any(marker in str(key).casefold() for marker in _SECRET_MARKERS) else cls._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(item) for item in value[:50]]
        if isinstance(value, str):
            return value[:2000]
        return value

    @staticmethod
    def _redact_error(value: str) -> str:
        """Keep audit errors useful without persisting common secret values."""

        redacted = value[:2000]
        for marker in _SECRET_MARKERS:
            redacted = re.sub(
                rf"(?i)({re.escape(marker)}\s*[:=]\s*)([^\s,;]+)",
                r"\1[redacted]",
                redacted,
            )
        return redacted

    @staticmethod
    def _audit_output(tool_name: str, output: Mapping[str, Any]) -> dict[str, Any]:
        if tool_name == "load_project_context":
            return {
                "project_id": output.get("project", {}).get("id") if isinstance(output.get("project"), Mapping) else None,
                "counts": output.get("counts", {}),
                "content_asset_id": output.get("content_asset", {}).get("id") if isinstance(output.get("content_asset"), Mapping) else None,
            }
        if tool_name == "retrieve_project_memories":
            memories = output.get("memories") if isinstance(output.get("memories"), list) else []
            return {"selected_count": len(memories), "memory_ids": [item.get("memory_id") for item in memories if isinstance(item, Mapping)]}
        if tool_name == "capture_gsc_performance":
            snapshots = output.get("snapshots") if isinstance(output.get("snapshots"), list) else []
            return {
                "captured_count": len(snapshots),
                "snapshot_ids": [item.get("snapshot_id") for item in snapshots if isinstance(item, Mapping)],
            }
        if tool_name == "update_memory_governance":
            memory_ids = output.get("memory_ids") if isinstance(output.get("memory_ids"), list) else []
            return {"memories_created": output.get("memories_created", 0), "memory_ids": memory_ids}
        return {"status": "completed"}
