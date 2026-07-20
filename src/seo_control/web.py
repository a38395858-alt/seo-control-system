"""Local static workspace with CSV-import and Google Suggest APIs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import json
import os
import re
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping
from urllib.parse import parse_qs, urlsplit

from seo_control.application.csv_keyword_import import parse_keyword_csv
from seo_control.application.ai_keyword_reviewer import OpenAICompatibleKeywordReviewer, RuleBasedKeywordReviewer
from seo_control.application.ai_title_generator import OpenAICompatibleTitleGenerator, RuleBasedTitleGenerator, TitleGenerationProtocolError
from seo_control.application.content_generator import ContentGenerationProtocolError, OpenAICompatibleContentGenerator, PROMPT_VERSION
from seo_control.application.browser_serp_title_client import BrowserSerpTitleClient, GoogleSerpProtocolError, GoogleSerpVerificationRequired
from seo_control.application.google_suggest_client import GoogleSuggestClient, GoogleSuggestProtocolError
from seo_control.application.keyword_expansion_service import KeywordExpansionService
from seo_control.application.keyword_import_service import KeywordImportService
from seo_control.domain.keywords import normalize_keyword
from seo_control.domain.keyword_scoring import KeywordScoringInput, calculate_keyword_score
from seo_control.infrastructure.database import initialize_database


WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
LOCAL_CONFIGURATION_FILE = Path(__file__).resolve().parents[2] / "配置文件.txt"
AI_SETTINGS_FILE = Path(__file__).resolve().parents[2] / "data" / "ai-settings.json"
AI_PROVIDERS = ("openai", "gemini", "deepseek")
DEFAULT_AI_ASSIGNMENTS = {"keyword_review": "openai", "title_generation": "openai", "content_generation": "openai"}
CONTENT_PROVIDER_LABELS = {"openai": "ChatGPT", "gemini": "Gemini", "deepseek": "DeepSeek"}


class KeywordDiscoveryServer(ThreadingHTTPServer):
    database_path: str | Path
    suggest_client: Any
    keyword_reviewer: Any | None
    title_generator: Any
    serp_title_client: Any
    ai_settings_path: Path
    content_generator: Any | None


class KeywordDiscoveryRequestHandler(SimpleHTTPRequestHandler):
    server: KeywordDiscoveryServer

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/keywords":
            self._list_keywords()
        elif path == "/api/projects":
            self._list_projects()
        elif path == "/api/settings/ai":
            self._get_ai_settings()
        elif path == "/api/title-library":
            self._list_title_library()
        elif path == "/api/content-assets":
            self._list_content_assets()
        elif path == "/api/content-library":
            self._list_content_library()
        elif self._content_asset_path(path) is not None:
            self._get_content_asset(self._content_asset_path(path) or 0)
        elif self._keyword_title_candidates_path(path) is not None:
            self._list_title_candidates(self._keyword_title_candidates_path(path) or 0)
        elif path in {"", "/", "/research", "/keywords", "/titles", "/title-library", "/content", "/content-library", "/scoring", "/settings"} or re.fullmatch(r"/content-library/\d+", path):
            self._serve_index()
        else:
            super().do_GET()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        candidate_id = self._title_candidate_action_path(path, "select")
        content_action = self._content_asset_action_path(path)
        if path not in {"/api/projects", "/api/keyword-imports", "/api/suggest-expansions", "/api/keyword-opportunity-scores", "/api/expanded-keywords", "/api/ai-keyword-reviews", "/api/serp-title-research", "/api/browser-serp-title-research", "/api/title-generation-jobs", "/api/multi-provider-title-generation-jobs", "/api/title-candidates", "/api/content-assets", "/api/settings/ai", "/api/settings/ai/test"} and candidate_id is None and content_action is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        payload = self._read_json()
        if payload is None:
            return
        if path == "/api/projects":
            self._create_project(payload)
        elif path == "/api/keyword-imports":
            self._import_keywords(payload)
        elif path == "/api/keyword-opportunity-scores":
            self._score_keyword_opportunities(payload)
        elif path == "/api/expanded-keywords":
            self._save_expanded_keywords(payload)
        elif path == "/api/ai-keyword-reviews":
            self._review_keyword(payload)
        elif path == "/api/serp-title-research":
            self._research_serp_titles(payload)
        elif path == "/api/browser-serp-title-research":
            self._research_browser_serp_titles(payload)
        elif path == "/api/title-generation-jobs":
            self._create_title_generation_job(payload)
        elif path == "/api/multi-provider-title-generation-jobs":
            self._create_multi_provider_title_job(payload)
        elif path == "/api/title-candidates":
            self._create_manual_title_candidate(payload)
        elif path == "/api/content-assets":
            self._create_content_asset(payload)
        elif content_action is not None:
            asset_id, action = content_action
            if action == "briefs": self._create_content_brief(asset_id, payload)
            elif action == "outlines": self._create_content_outline(asset_id, payload)
            else: self._generate_content(asset_id, action, payload)
        elif path == "/api/settings/ai":
            self._save_ai_settings(payload)
        elif path == "/api/settings/ai/test":
            self._test_ai_settings(payload)
        elif candidate_id is not None:
            self._select_title_candidate(candidate_id, payload)
        else:
            self._expand_suggestions(payload)

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        candidate_id = self._title_candidate_path(path)
        content_asset_id = self._content_asset_path(path)
        if path not in {"/api/keywords", "/api/content-assets", "/api/title-candidates"} and candidate_id is None and content_asset_id is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        payload = self._read_json()
        if payload is not None and candidate_id is not None:
            self._delete_title_candidate(candidate_id, payload)
        elif payload is not None and path == "/api/title-candidates":
            self._delete_title_candidates(payload)
        elif payload is not None and content_asset_id is not None:
            self._delete_content_assets(payload, asset_id=content_asset_id)
        elif payload is not None and path == "/api/content-assets":
            self._delete_content_assets(payload)
        elif payload is not None:
            self._delete_keywords(payload)

    def _serve_index(self) -> None:
        try:
            content = (WEB_ROOT / "index.html").read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND, "Keyword-discovery page not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _create_content_asset(self, payload: Mapping[str, Any]) -> None:
        project_id, title_id = self._integer(payload, "project_id"), self._integer(payload, "selected_title_candidate_id")
        if project_id is None or title_id is None: return
        try:
            with self._database() as connection:
                title = connection.execute("SELECT * FROM keyword_title_candidates WHERE id=? AND project_id=? AND status='selected' AND deleted_at IS NULL", (title_id, project_id)).fetchone()
                if title is None: raise ValueError("an active selected title is required to create content")
                keyword = self._title_keyword(connection, project_id, title["keyword_id"], require_approved=True)
                existing = connection.execute("SELECT * FROM content_assets WHERE project_id=? AND selected_title_candidate_id=? AND deleted_at IS NULL", (project_id, title_id)).fetchone()
                if existing is not None:
                    self._json(HTTPStatus.OK, self._content_asset_payload(existing))
                    return
                with connection:
                    cursor = connection.execute("INSERT INTO content_assets(project_id,keyword_id,selected_title_candidate_id,title_snapshot,locale,country_code,content_type) VALUES(?,?,?,?,?,?,?)", (project_id, keyword["id"], title_id, title["title"], f"{keyword['language_code']}-{keyword['country_code']}", keyword["country_code"], self._optional_text(payload, "content_type") or "guide"))
                row = connection.execute("SELECT * FROM content_assets WHERE id=?", (cursor.lastrowid,)).fetchone()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.CREATED, self._content_asset_payload(row))

    def _list_content_assets(self) -> None:
        values = parse_qs(urlsplit(self.path).query).get("project_id", [])
        if len(values) != 1: self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id is required."}); return
        with self._database() as connection:
            rows = connection.execute("SELECT assets.*, keywords.keyword FROM content_assets assets JOIN keywords ON keywords.id=assets.keyword_id WHERE assets.project_id=? AND assets.deleted_at IS NULL ORDER BY assets.updated_at DESC, assets.id DESC", (int(values[0]),)).fetchall()
        self._json(HTTPStatus.OK, [self._content_asset_payload(row) for row in rows])

    def _list_content_library(self) -> None:
        values = parse_qs(urlsplit(self.path).query).get("project_id", [])
        if len(values) != 1:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id is required."}); return
        try:
            project_id = int(values[0])
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id must be an integer."}); return
        with self._database() as connection:
            rows = connection.execute(
                """SELECT assets.*, keywords.keyword, drafts.version AS current_draft_version,
                          drafts.meta_description, drafts.qa_status, drafts.provider, drafts.model
                   FROM content_assets AS assets
                   JOIN keywords ON keywords.id=assets.keyword_id
                   JOIN content_drafts AS drafts ON drafts.id=assets.current_draft_id
                   WHERE assets.project_id=? AND assets.deleted_at IS NULL AND assets.current_draft_id IS NOT NULL
                   ORDER BY assets.updated_at DESC, assets.id DESC""",
                (project_id,),
            ).fetchall()
        self._json(HTTPStatus.OK, [self._content_asset_payload(row) for row in rows])

    def _get_content_asset(self, asset_id: int) -> None:
        values = parse_qs(urlsplit(self.path).query).get("project_id", [])
        if len(values) != 1: self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id is required."}); return
        with self._database() as connection:
            payload = self._content_asset_detail(connection, int(values[0]), asset_id)
        self._json(HTTPStatus.OK, payload)

    def _create_content_brief(self, asset_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        audience, goal = self._text(payload, "target_audience"), self._text(payload, "business_goal")
        target = 0 if "target_length" not in payload else self._integer(payload, "target_length")
        sources = payload.get("sources", [])
        if project_id is None or target is None or not isinstance(sources, list): return
        try:
            with self._database() as connection:
                self._content_asset(connection, project_id, asset_id)
                with connection:
                    connection.execute("UPDATE content_briefs SET status='superseded' WHERE content_asset_id=? AND status='current'", (asset_id,))
                    cursor = connection.execute("INSERT INTO content_briefs(content_asset_id,target_audience,business_goal,target_length,sources_json,brief_json) VALUES(?,?,?,?,?,?)", (asset_id, audience, goal, target, json.dumps(sources, ensure_ascii=False), json.dumps({"source_policy": "unavailable sources must be marked [VERIFY]"})))
                    connection.execute("UPDATE content_assets SET status='briefing',current_brief_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (cursor.lastrowid, asset_id))
                    row = connection.execute("SELECT * FROM content_briefs WHERE id=?", (cursor.lastrowid,)).fetchone()
        except (sqlite3.Error, ValueError) as error: self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.CREATED, self._content_brief_payload(row))

    def _create_content_outline(self, asset_id: int, payload: Mapping[str, Any]) -> None:
        project_id, sections = self._integer(payload, "project_id"), payload.get("sections")
        if project_id is None or not isinstance(sections, list) or not sections: self._json(HTTPStatus.BAD_REQUEST, {"error": "sections are required."}); return
        try:
            with self._database() as connection:
                asset = self._content_asset(connection, project_id, asset_id)
                brief_id = asset["current_brief_id"]
                if brief_id is None: raise ValueError("a content brief is required before creating an outline")
                with connection:
                    cursor = connection.execute("INSERT INTO content_outlines(content_asset_id,brief_id) VALUES(?,?)", (asset_id, brief_id))
                    for position, section in enumerate(sections, 1):
                        if not isinstance(section, Mapping) or not all(isinstance(section.get(key), str) and section[key].strip() for key in ("heading", "purpose")): raise ValueError("each outline section needs heading and purpose")
                        section_data = self._normalise_outline_section(section, position)
                        connection.execute("INSERT INTO content_outline_sections(outline_id,position,heading,purpose,word_budget,section_json) VALUES(?,?,?,?,?,?)", (cursor.lastrowid, position, section_data["heading"], section_data["purpose"], 0, json.dumps(section_data, ensure_ascii=False)))
                    connection.execute("UPDATE content_assets SET status='outlining',current_outline_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (cursor.lastrowid, asset_id))
                    row = connection.execute("SELECT * FROM content_outlines WHERE id=?", (cursor.lastrowid,)).fetchone()
                    result = self._content_outline_payload(connection, row)
        except (sqlite3.Error, ValueError) as error: self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.CREATED, result)

    def _generate_content(self, asset_id: int, action: str, payload: Mapping[str, Any]) -> None:
        """Run one or all evidence-grounded synthesis stages and persist every result."""
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        job_id: int | None = None
        provider = self._optional_text(payload, "provider") or "openai"
        model: str | None = None
        try:
            with self._database() as connection:
                asset = self._content_asset(connection, project_id, asset_id)
                generator, provider, model = self._content_generator(payload)
                job_id = self._start_content_generation_job(connection, asset, action, provider, model)
                if generator is None:
                    job = self._finish_content_generation_job(connection, job_id, status="failed", failed_stage="configuration", error_summary="Selected provider is not configured.")
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"{self._content_provider_label(provider)} configuration 未配置，未切换到其他模型。", "generation_job": job})
                    return
                result: dict[str, Any] = {}
                # A one-click run respects an existing user-approved Brief.  It only
                # creates a Brief when there is no fact boundary to carry forward.
                if action == "generate-brief" or (action == "generate" and asset["current_brief_id"] is None):
                    result["brief"] = self._generate_ai_brief(connection, asset, payload, generator, provider, model, job_id)
                    asset = self._content_asset(connection, project_id, asset_id)
                if action in {"generate-outline", "generate"}:
                    result["outline"] = self._generate_ai_outline(connection, asset, payload, generator, provider, model, job_id)
                    asset = self._content_asset(connection, project_id, asset_id)
                if action in {"generate-draft", "generate"}:
                    result["draft"] = self._generate_ai_draft(connection, asset, payload, generator, provider, model, job_id)
                result["generation_job"] = self._finish_content_generation_job(connection, job_id, status="completed")
                refreshed = self._content_asset_detail(connection, project_id, asset_id)
                result["runs"] = refreshed["generation_runs"]
        except ContentGenerationProtocolError as error:
            detail = str(error) or "AI content generation returned invalid JSON."
            stage = self._content_failed_stage(detail)
            job = None
            if job_id is not None:
                with self._database() as connection:
                    job = self._finish_content_generation_job(connection, job_id, status="failed", failed_stage=stage, error_summary=detail)
            self._json(HTTPStatus.BAD_GATEWAY, {"error": f"{self._content_provider_label(provider)} 内容生成在 {self._content_stage_label(stage)} 阶段失败：{detail}。未切换到其他模型，旧正文已保留。", "generation_job": job})
            return
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as error:
            job = None
            if job_id is not None:
                with self._database() as connection:
                    job = self._finish_content_generation_job(connection, job_id, status="failed", failed_stage="preparation", error_summary=str(error))
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error), "generation_job": job})
            return
        self._json(HTTPStatus.CREATED, result)

    @staticmethod
    def _content_provider_label(provider: str) -> str:
        return CONTENT_PROVIDER_LABELS.get(provider, provider)

    @staticmethod
    def _content_stage_label(stage: str) -> str:
        return {"semantic": "语义分析", "title": "标题与元信息", "outline": "文章大纲", "section": "章节写作", "assembly": "组装全文", "configuration": "模型配置", "preparation": "任务准备"}.get(stage, stage)

    @staticmethod
    def _content_failed_stage(error: str) -> str:
        matched = re.search(r"AI content (semantic|title|outline|section|assembly)", error)
        return matched.group(1) if matched else "generation"

    @staticmethod
    def _start_content_generation_job(connection: sqlite3.Connection, asset: sqlite3.Row, action: str, provider: str, model: str | None) -> int:
        with connection:
            cursor = connection.execute(
                "INSERT INTO content_generation_jobs(project_id,content_asset_id,requested_action,provider,model) VALUES(?,?,?,?,?)",
                (asset["project_id"], asset["id"], action, provider, model),
            )
        return int(cursor.lastrowid)

    @staticmethod
    def _finish_content_generation_job(connection: sqlite3.Connection, job_id: int, *, status: str, failed_stage: str | None = None, error_summary: str | None = None) -> dict[str, Any]:
        with connection:
            connection.execute(
                "UPDATE content_generation_jobs SET status=?,failed_stage=?,error_summary=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, failed_stage, error_summary, job_id),
            )
        row = connection.execute("SELECT * FROM content_generation_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row)

    def _content_generator(self, payload: Mapping[str, Any]) -> tuple[Any | None, str, str | None]:
        requested = self._optional_text(payload, "provider")
        if requested is not None and requested not in AI_PROVIDERS:
            raise ValueError("provider must be openai, gemini, or deepseek")
        injected = getattr(self.server, "content_generator", None)
        if injected is not None:
            return injected, requested or str(getattr(injected, "provider", "custom")), getattr(injected, "model", None)
        provider = requested or _ai_assignments(self.server.ai_settings_path).get("content_generation", "openai")
        if not getattr(self.server, "allow_environment_ai_fallback", True) and not _ai_settings_document(self.server.ai_settings_path).get("providers"):
            return None, provider, None
        configuration = _provider_configuration(self.server.ai_settings_path, provider)
        if configuration is None:
            return None, provider, None
        api_key, base_url, model = configuration
        return OpenAICompatibleContentGenerator(api_key, base_url, model, provider=provider), provider, model

    def _generate_ai_brief(self, connection: sqlite3.Connection, asset: sqlite3.Row, payload: Mapping[str, Any], generator: Any, provider: str, model: str | None, generation_job_id: int) -> dict[str, Any]:
        audience = self._optional_text(payload, "target_audience") or "US searchers evaluating this topic"
        goal = self._optional_text(payload, "business_goal") or "informational"
        sources = self._content_sources(payload.get("sources", []))
        data = {"topic": asset["title_snapshot"], "primary_keyword": asset["keyword"], "language_market": asset["locale"], "audience": audience, "business_goal": goal, "brand": self._optional_text(payload, "brand") or "", "sources": sources, "constraints": payload.get("constraints", [])}
        semantic, _run = self._run_content_stage(connection, asset, "semantic", data, generator, provider, model, generation_job_id)
        brief_json = {"semantic": semantic, "source_policy": "Material facts without a usable source must be marked [VERIFY]."}
        with connection:
            connection.execute("UPDATE content_briefs SET status='superseded' WHERE content_asset_id=? AND status='current'", (asset["id"],))
            cursor = connection.execute("INSERT INTO content_briefs(content_asset_id,target_audience,business_goal,target_length,sources_json,brief_json) VALUES(?,?,?,?,?,?)", (asset["id"], audience, goal, 0, json.dumps(sources, ensure_ascii=False), json.dumps(brief_json, ensure_ascii=False)))
            connection.execute("UPDATE content_assets SET status='briefing',current_brief_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (cursor.lastrowid, asset["id"]))
            row = connection.execute("SELECT * FROM content_briefs WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._content_brief_payload(row)

    def _generate_ai_outline(self, connection: sqlite3.Connection, asset: sqlite3.Row, payload: Mapping[str, Any], generator: Any, provider: str, model: str | None, generation_job_id: int) -> dict[str, Any]:
        brief = self._current_content_brief(connection, asset)
        semantic = self._content_brief_payload(brief)["brief"].get("semantic", {})
        title_data = {"semantic": semantic, "primary_keyword": asset["keyword"], "title_snapshot": asset["title_snapshot"], "voice": self._optional_text(payload, "voice") or "clear, helpful American English", "year_rule": "none"}
        metadata, _run = self._run_content_stage(connection, asset, "title", title_data, generator, provider, model, generation_job_id)
        if not isinstance(metadata.get("selected_title"), str) or not metadata["selected_title"].strip():
            metadata["selected_title"] = asset["title_snapshot"]
        metadata["selected_title"] = asset["title_snapshot"]  # The user-approved title is canonical.
        updated_brief = self._content_brief_payload(brief)["brief"]
        updated_brief["metadata"] = metadata
        with connection:
            connection.execute("UPDATE content_briefs SET brief_json=? WHERE id=?", (json.dumps(updated_brief, ensure_ascii=False), brief["id"]))
        outline_data = {"semantic": semantic, "metadata": metadata, "cta": self._optional_text(payload, "cta") or "", "sources": self._content_brief_payload(brief)["sources"]}
        outline_json, _run = self._run_content_stage(connection, asset, "outline", outline_data, generator, provider, model, generation_job_id)
        sections = outline_json.get("sections")
        if not isinstance(sections, list) or not sections:
            raise ContentGenerationProtocolError("AI content outline returned no usable sections.")
        with connection:
            cursor = connection.execute("INSERT INTO content_outlines(content_asset_id,brief_id,status) VALUES(?,?,?)", (asset["id"], brief["id"], "approved"))
            for position, section in enumerate(sections, 1):
                if not isinstance(section, Mapping) or not isinstance(section.get("heading"), str) or not section["heading"].strip():
                    raise ContentGenerationProtocolError("AI content outline has an invalid section.")
                section_data = self._normalise_outline_section(section, position)
                connection.execute("INSERT INTO content_outline_sections(outline_id,position,heading,purpose,word_budget,section_json) VALUES(?,?,?,?,?,?)", (cursor.lastrowid, position, section_data["heading"], section_data["purpose"], 0, json.dumps(section_data, ensure_ascii=False)))
            connection.execute("UPDATE content_assets SET status='outlining',current_outline_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (cursor.lastrowid, asset["id"]))
            row = connection.execute("SELECT * FROM content_outlines WHERE id=?", (cursor.lastrowid,)).fetchone()
        result = self._content_outline_payload(connection, row)
        result["blueprint"] = outline_json
        return result

    def _generate_ai_draft(self, connection: sqlite3.Connection, asset: sqlite3.Row, payload: Mapping[str, Any], generator: Any, provider: str, model: str | None, generation_job_id: int) -> dict[str, Any]:
        brief = self._current_content_brief(connection, asset)
        if asset["current_outline_id"] is None:
            raise ValueError("an AI outline is required before generating a draft")
        outline = connection.execute("SELECT * FROM content_outlines WHERE id=?", (asset["current_outline_id"],)).fetchone()
        if outline is None:
            raise ValueError("current content outline does not exist")
        brief_data = self._content_brief_payload(brief)
        semantic = brief_data["brief"].get("semantic", {})
        metadata = brief_data["brief"].get("metadata", {"selected_title": asset["title_snapshot"], "meta_description": ""})
        if not isinstance(metadata, Mapping): metadata = {"selected_title": asset["title_snapshot"], "meta_description": ""}
        outline_payload = self._content_outline_payload(connection, outline)
        blueprint = {"sections": outline_payload["sections"]}
        section_drafts: list[dict[str, Any]] = []
        for section in blueprint["sections"]:
            section_data = {"topic": asset["title_snapshot"], "audience": brief["target_audience"], "intent": semantic.get("intent", {}), "angle": semantic.get("angle", ""), "title": metadata.get("selected_title", asset["title_snapshot"]), "section": {"id": f"s{section['position']}", **section}, "sources": brief_data["sources"], "voice": self._optional_text(payload, "voice") or "clear, helpful American English", "language": asset["locale"]}
            drafted, _run = self._run_content_stage(connection, asset, "section", section_data, generator, provider, model, generation_job_id)
            if not isinstance(drafted.get("markdown"), str):
                raise ContentGenerationProtocolError("AI content section returned no Markdown.")
            section_drafts.append(drafted)
        assembly_data = {"metadata": dict(metadata), "outline": blueprint, "section_drafts": section_drafts, "brand": self._optional_text(payload, "brand") or "", "cta": self._optional_text(payload, "cta") or ""}
        article, assembly_run = self._run_content_stage(connection, asset, "assembly", assembly_data, generator, provider, model, generation_job_id)
        if not isinstance(article.get("markdown"), str):
            raise ContentGenerationProtocolError("AI content assembly returned no Markdown.")
        markdown = article["markdown"]
        verification = article.get("verify", []) if isinstance(article.get("verify", []), list) else []
        compatibility_qa = {"status": "not_run", "checks": [], "unresolved_verify": verification}
        with connection:
            version = int(connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM content_drafts WHERE content_asset_id=?", (asset["id"],)).fetchone()[0])
            cursor = connection.execute("INSERT INTO content_drafts(project_id,content_asset_id,outline_id,generation_run_id,generation_job_id,version,title,meta_description,markdown,sources_used_json,unresolved_verify_json,qa_json,qa_status,provider,model) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (asset["project_id"], asset["id"], outline["id"], assembly_run["id"], generation_job_id, version, str(article.get("title") or metadata.get("selected_title") or asset["title_snapshot"]), str(article.get("meta_description") or metadata.get("meta_description") or ""), markdown, json.dumps(article.get("sources_used", []), ensure_ascii=False), json.dumps(verification, ensure_ascii=False), json.dumps(compatibility_qa, ensure_ascii=False), "not_run", provider, model))
            connection.execute("UPDATE content_assets SET status='in_review',current_draft_id=?,current_generation_run_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (cursor.lastrowid, assembly_run["id"], asset["id"]))
            draft = connection.execute("SELECT * FROM content_drafts WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._content_draft_payload(draft)

    def _run_content_stage(self, connection: sqlite3.Connection, asset: sqlite3.Row, stage: str, data: Mapping[str, Any], generator: Any, provider: str, model: str | None, generation_job_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        with connection:
            cursor = connection.execute("INSERT INTO content_generation_runs(project_id,content_asset_id,stage,provider,model,generation_job_id,status,input_json,prompt_version) VALUES(?,?,?,?,?,?,'running',?,?)", (asset["project_id"], asset["id"], stage, provider, model, generation_job_id, json.dumps(data, ensure_ascii=False), PROMPT_VERSION))
            run_id = int(cursor.lastrowid)
        try:
            if callable(getattr(generator, "run_stage", None)):
                raw = generator.run_stage(stage=stage, data=dict(data))
            elif callable(getattr(generator, "generate", None)):
                raw = generator.generate(stage=stage, **dict(data))
            else:
                raise ContentGenerationProtocolError("configured content generator has no supported stage method")
            result = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(result, Mapping):
                raise ContentGenerationProtocolError(f"AI content {stage} returned invalid JSON.")
            value = dict(result)
        except Exception as error:
            with connection:
                connection.execute("UPDATE content_generation_runs SET status='failed',error_summary=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (str(error) or f"AI content {stage} failed.", run_id))
            if isinstance(error, ContentGenerationProtocolError):
                raise
            raise ContentGenerationProtocolError(f"AI content {stage} request failed.") from error
        with connection:
            connection.execute("UPDATE content_generation_runs SET status='completed',output_json=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(value, ensure_ascii=False), run_id))
        return value, {"id": run_id, "stage": stage, "status": "completed"}

    @staticmethod
    def _normalise_outline_section(section: Mapping[str, Any], position: int) -> dict[str, Any]:
        heading = section.get("heading")
        if not isinstance(heading, str) or not heading.strip():
            raise ContentGenerationProtocolError("AI content outline has an invalid section.")
        purpose = section.get("purpose") if isinstance(section.get("purpose"), str) and section["purpose"].strip() else "Advance the reader decision."
        def strings(field: str) -> list[str]:
            value = section.get(field, [])
            return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []
        level = section.get("level") if section.get("level") in {"h2", "h3"} else "h2"
        format_value = section.get("format") if section.get("format") in {"paragraphs", "list", "table"} else "paragraphs"
        return {
            "id": section.get("id") if isinstance(section.get("id"), str) and section["id"].strip() else f"s{position}",
            "heading": heading.strip(), "level": level,
            "reader_question": section.get("reader_question") if isinstance(section.get("reader_question"), str) else "",
            "purpose": purpose.strip(), "key_points": strings("key_points"), "source_ids": strings("source_ids"),
            "evidence_gaps": strings("evidence_gaps"), "format": format_value,
        }

    @staticmethod
    def _content_sources(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError("sources must be an array")
        normalized: list[dict[str, Any]] = []
        for position, source in enumerate(value, 1):
            if isinstance(source, str):
                content = source.strip()
                if not content:
                    raise ValueError("source text must not be empty")
                normalized.append({"source_id": f"source-{position}", "source_type": "note", "content": content, "availability": "available"})
                continue
            if not isinstance(source, Mapping):
                raise ValueError("each source must be a string or structured object")
            item = dict(source)
            source_id = item.get("source_id")
            source_type = item.get("source_type")
            availability = item.get("availability")
            if not isinstance(source_id, str) or not source_id.strip():
                raise ValueError("structured source.source_id is required")
            if not isinstance(source_type, str) or not source_type.strip():
                raise ValueError("structured source.source_type is required")
            if availability not in {"available", "unavailable"}:
                raise ValueError("structured source.availability must be available or unavailable")
            if source_type == "url" and availability == "available":
                url = item.get("url")
                if not isinstance(url, str) or not url.strip().startswith(("https://", "http://")):
                    raise ValueError("available URL source.url is required")
            normalized.append(item)
        return normalized

    @staticmethod
    def _integer_or_default(payload: Mapping[str, Any], field: str, default: int) -> int:
        value = payload.get(field, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 100:
            raise ValueError(f"{field} must be an integer of at least 100")
        return value

    @staticmethod
    def _current_content_brief(connection: sqlite3.Connection, asset: sqlite3.Row) -> sqlite3.Row:
        if asset["current_brief_id"] is None:
            raise ValueError("an AI content brief is required before generating an outline")
        brief = connection.execute("SELECT * FROM content_briefs WHERE id=?", (asset["current_brief_id"],)).fetchone()
        if brief is None:
            raise ValueError("current content brief does not exist")
        return brief

    def _create_project(self, payload: Mapping[str, Any]) -> None:
        name = self._text(payload, "name")
        if name is None:
            return
        country = self._optional_text(payload, "country_code") or "US"
        language = self._optional_text(payload, "language_code") or "en"
        try:
            with self._database() as connection:
                project_id = KeywordImportService(connection).create_project(name, country, language)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.CREATED, {"id": project_id})

    def _list_projects(self) -> None:
        with self._database() as connection:
            rows = connection.execute(
                "SELECT id,name,site_url,default_country,default_language,created_at FROM projects ORDER BY id DESC"
            ).fetchall()
        self._json(HTTPStatus.OK, [dict(row) for row in rows])

    def _get_ai_settings(self) -> None:
        assignments = _ai_assignments(self.server.ai_settings_path)
        configuration = _ai_configuration(self.server.ai_settings_path, purpose="keyword_review")
        providers = _public_ai_profiles(self.server.ai_settings_path)
        if configuration is None:
            self._json(HTTPStatus.OK, {"configured": False, "base_url": None, "model": None, "provider": None, "providers": providers, "assignments": assignments})
            return
        _key, base_url, model = configuration
        provider = assignments["keyword_review"]
        self._json(HTTPStatus.OK, {"configured": True, "base_url": base_url, "model": model, "provider": provider, "providers": providers, "assignments": assignments})

    def _save_ai_settings(self, payload: Mapping[str, Any]) -> None:
        if "providers" in payload:
            self._save_ai_provider_profiles(payload)
            return
        api_key = self._text(payload, "api_key")
        base_url = self._text(payload, "base_url")
        model = self._text(payload, "model")
        if None in {api_key, base_url, model}:
            return
        if not base_url.startswith(("https://", "http://")):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "base_url must start with http:// or https://."})
            return
        try:
            self.server.ai_settings_path.parent.mkdir(parents=True, exist_ok=True)
            provider = self._optional_text(payload, "provider") or _provider_for_base_url(base_url)
            if provider not in AI_PROVIDERS:
                provider = "openai"
            assignments = _ai_assignments(self.server.ai_settings_path)
            assignments.update({"keyword_review": provider, "title_generation": provider})
            document = {"providers": {provider: {"api_key": api_key, "base_url": base_url.rstrip("/"), "model": model}}, "assignments": assignments}
            self.server.ai_settings_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            self.server.keyword_reviewer = _default_keyword_reviewer(self.server.ai_settings_path)
            self.server.title_generator = _default_title_generator(self.server.ai_settings_path)
        except OSError as error:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
            return
        self._get_ai_settings()

    def _save_ai_provider_profiles(self, payload: Mapping[str, Any]) -> None:
        raw_profiles = payload.get("providers")
        raw_assignments = payload.get("assignments")
        if not isinstance(raw_profiles, Mapping) or not isinstance(raw_assignments, Mapping):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "providers and assignments are required."})
            return
        assignments = _normalize_ai_assignments(raw_assignments)
        if assignments is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid AI feature assignment."})
            return
        # A provider card may be saved on its own. Start with all existing
        # profiles, then replace only the profiles included by the request.
        # This avoids erasing a different provider's saved key.
        profiles: dict[str, dict[str, str]] = {}
        for provider in AI_PROVIDERS:
            raw = raw_profiles.get(provider)
            current = _provider_configuration(self.server.ai_settings_path, provider)
            if raw is None:
                if current is not None:
                    profiles[provider] = {"api_key": current[0], "base_url": current[1], "model": current[2]}
                continue
            if not isinstance(raw, Mapping):
                self._json(HTTPStatus.BAD_REQUEST, {"error": f"providers.{provider} must be an object."})
                return
            requested_key = raw.get("api_key") if isinstance(raw.get("api_key"), str) else ""
            requested_base_url = raw.get("base_url") if isinstance(raw.get("base_url"), str) else ""
            requested_model = raw.get("model") if isinstance(raw.get("model"), str) else ""
            api_key = requested_key or (current[0] if current else "")
            base_url = requested_base_url or (current[1] if current else "")
            model = requested_model or (current[2] if current else "")
            api_key, base_url, model = api_key.strip(), _normalize_ai_base_url(base_url), model.strip()
            if not api_key and current is None:
                continue
            if not any((api_key, base_url, model)):
                continue
            if not all((api_key, base_url, model)) or not base_url.startswith(("https://", "http://")):
                self._json(HTTPStatus.BAD_REQUEST, {"error": f"Complete API Key, endpoint and model for {provider}."})
                return
            profiles[provider] = {"api_key": api_key, "base_url": base_url, "model": model}
        self.server.ai_settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.server.ai_settings_path.write_text(json.dumps({"providers": profiles, "assignments": assignments}, ensure_ascii=False), encoding="utf-8")
        self.server.keyword_reviewer = _default_keyword_reviewer(self.server.ai_settings_path)
        self.server.title_generator = _default_title_generator(self.server.ai_settings_path)
        self._get_ai_settings()

    def _test_ai_settings(self, payload: Mapping[str, Any]) -> None:
        provider = self._optional_text(payload, "provider")
        raw_config = payload.get("config")
        configuration = self._temporary_ai_configuration(raw_config) if isinstance(raw_config, Mapping) else (_provider_configuration(self.server.ai_settings_path, provider) if provider in AI_PROVIDERS else _ai_configuration(self.server.ai_settings_path, purpose="title_generation"))
        if configuration is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "请先保存 API Key、接口地址和模型名称。"})
            return
        _key, base_url, model = configuration
        try:
            generator = OpenAICompatibleTitleGenerator(*configuration) if provider in AI_PROVIDERS else self.server.title_generator
            result = generator.generate(
                keyword="SEO tools",
                locale="en-US",
                count=1,
                search_intent="informational",
                category="connection test",
                title_type="tutorial",
                competitor_titles=[],
            )
            self._title_candidates_from_response(result, 1)
        except Exception:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": "AI 连接测试失败，请检查接口地址、模型名称和 API Key。"})
            return
        active_provider = provider if provider in AI_PROVIDERS else _ai_assignments(self.server.ai_settings_path)["title_generation"]
        self._json(HTTPStatus.OK, {"status": "connected", "provider": active_provider, "model": model})

    @staticmethod
    def _temporary_ai_configuration(value: Mapping[str, Any]) -> tuple[str, str, str] | None:
        api_key = value.get("apiKey") if isinstance(value.get("apiKey"), str) else value.get("api_key")
        base_url = value.get("baseUrl") if isinstance(value.get("baseUrl"), str) else value.get("base_url")
        model = value.get("model")
        if not all(isinstance(item, str) and item.strip() for item in (api_key, base_url, model)):
            return None
        normalized_base_url = _normalize_ai_base_url(base_url)
        if not normalized_base_url.startswith(("https://", "http://")):
            return None
        return api_key.strip(), normalized_base_url, model.strip()

    def _expand_suggestions(self, payload: Mapping[str, Any]) -> None:
        seeds = payload.get("seed_keywords")
        if not isinstance(seeds, list) or not all(isinstance(seed, str) for seed in seeds):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "seed_keywords must be a string list."})
            return
        hl = self._text(payload, "hl")
        gl = self._text(payload, "gl")
        if hl is None or gl is None:
            return
        try:
            result = KeywordExpansionService(self.server.suggest_client).expand(
                seeds,
                hl=hl,
                gl=gl,
                max_keywords=self._limit(payload, "max_keywords", 5000),
                max_requests=self._limit(payload, "max_requests", 1000),
                max_depth=self._limit(payload, "max_depth", 10),
            )
        except (GoogleSuggestProtocolError, OSError, ValueError, TypeError) as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        self._json(
            HTTPStatus.OK,
            {
                "keywords": result.keywords,
                "requests_made": result.requests_made,
                "stop_reason": result.stop_reason,
                "debug_logs": result.debug_logs,
            },
        )

    def _score_keyword_opportunities(self, payload: Mapping[str, Any]) -> None:
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "items must be a non-empty list."})
            return
        scores: list[dict[str, Any]] = []
        try:
            for item in items:
                if not isinstance(item, Mapping):
                    raise ValueError("Each item must be an object.")
                keyword = self._score_keyword(item)
                values = KeywordScoringInput(
                    monthly_search_volume=self._score_integer(item, "monthly_search_volume"),
                    average_domain_authority=self._score_number(item, "average_domain_authority"),
                    average_referring_domains=self._score_number(item, "average_referring_domains"),
                    exact_title_match_rate=self._score_number(item, "exact_title_match_rate"),
                    authority_site_ratio=self._score_number(item, "authority_site_ratio"),
                    intent_competition=self._score_integer(item, "intent_competition"),
                    relevance_score=self._score_number(item, "relevance_score"),
                    business_value_score=self._score_number(item, "business_value_score"),
                )
                score = calculate_keyword_score(values)
                scores.append({"keyword": keyword, "keyword_difficulty": score.keyword_difficulty, "difficulty_level": score.difficulty_level, "opportunity_score": score.opportunity_score})
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {"scores": scores})

    def _save_expanded_keywords(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        seed_keyword = self._text(payload, "seed_keyword")
        country = self._text(payload, "country_code")
        language = self._text(payload, "language_code")
        items = payload.get("keywords")
        if None in {project_id, seed_keyword, country, language} or not isinstance(items, list):
            if isinstance(items, list):
                return
            self._json(HTTPStatus.BAD_REQUEST, {"error": "keywords must be a list."})
            return
        inserted = existing = 0
        try:
            with self._database() as connection:
                if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                    raise ValueError(f"project {project_id} does not exist")
                for item in items:
                    if not isinstance(item, Mapping):
                        raise ValueError("Each keyword must be an object.")
                    keyword = self._score_keyword(item)
                    keyword_id = self._upsert_expanded_keyword(connection, project_id, keyword, country, language, seed_keyword, item)
                    if keyword_id[1]:
                        inserted += 1
                    else:
                        existing += 1
                connection.commit()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.CREATED, {"inserted": inserted, "existing": existing})

    def _review_keyword(self, payload: Mapping[str, Any]) -> None:
        seed_keyword = self._text(payload, "seed_keyword")
        keyword = self._text(payload, "keyword")
        language = self._text(payload, "language")
        if None in {seed_keyword, keyword, language}:
            return
        mode = self._optional_text(payload, "mode") or "hybrid"
        if mode not in {"fast", "hybrid"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "mode must be fast or hybrid."})
            return
        reviewer = self.server.keyword_reviewer
        if reviewer is None:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "AI keyword reviewer is not configured."})
            return
        local_review = RuleBasedKeywordReviewer().review(seed_keyword=seed_keyword, keyword=keyword, language=language)
        if mode == "fast" or not (local_review.is_seo_content_fit and local_review.same_topic_as_seed):
            response: dict[str, Any] = {"review": local_review.as_dict(), "provider": "rule", "mode": mode}
            if mode == "hybrid":
                response["warning"] = "本地规则判定为低相关，已跳过 AI 请求。"
            self._json(HTTPStatus.OK, response)
            return
        fallback_warning = None
        try:
            review = reviewer.review(seed_keyword=seed_keyword, keyword=keyword, language=language)
            if hasattr(review, "as_dict"):
                review = review.as_dict()
            if not isinstance(review, Mapping):
                raise ValueError("AI reviewer must return an object.")
        except Exception:
            review = RuleBasedKeywordReviewer().review(seed_keyword=seed_keyword, keyword=keyword, language=language).as_dict()
            fallback_warning = "AI 返回异常，已用本地规则完成本词初筛。"
        response: dict[str, Any] = {"review": dict(review), "provider": "ai" if fallback_warning is None else "rule_fallback", "mode": mode}
        if fallback_warning:
            response["warning"] = fallback_warning
        self._json(HTTPStatus.OK, response)

    def _create_title_generation_job(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        keyword_id = self._integer(payload, "keyword_id")
        locale = self._optional_text(payload, "locale") or "en-US"
        count = payload.get("count", 8)
        if project_id is None or keyword_id is None:
            return
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 20:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "count must be an integer from 1 to 20."})
            return
        title_type = self._optional_text(payload, "title_type")
        raw_competitor_titles = payload.get("competitor_titles", [])
        if not isinstance(raw_competitor_titles, list) or any(not isinstance(title, str) for title in raw_competitor_titles):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "competitor_titles must be a string list."})
            return
        competitor_titles = [" ".join(title.split()) for title in raw_competitor_titles if title.strip()][:20]
        try:
            with self._database() as connection:
                keyword = self._title_keyword(connection, project_id, keyword_id, require_approved=True)
                intent = connection.execute("SELECT search_intent FROM keyword_reviews WHERE keyword_id=? ORDER BY id DESC LIMIT 1", (keyword_id,)).fetchone()
                category = connection.execute("SELECT categories.name FROM keyword_category_assignments assignments JOIN keyword_categories categories ON categories.id=assignments.category_id WHERE assignments.keyword_id=? ORDER BY assignments.created_at DESC LIMIT 1", (keyword_id,)).fetchone()
                request_data = {"keyword": keyword["keyword"], "locale": locale, "count": count, "search_intent": intent[0] if intent else None, "category": category[0] if category else None, "title_type": title_type, "competitor_titles": competitor_titles}
                provider = "ai" if isinstance(self.server.title_generator, OpenAICompatibleTitleGenerator) else "rule"
                with connection:
                    cursor = connection.execute(
                        """INSERT INTO title_generation_jobs(project_id,keyword_id,status,request_json,provider,requested_count,started_at)
                           VALUES(?,?, 'running', ?,?,?, CURRENT_TIMESTAMP)""",
                        (project_id, keyword_id, json.dumps(request_data, ensure_ascii=False), provider, count),
                    )
                    job_id = int(cursor.lastrowid)
                    raw = self.server.title_generator.generate(**request_data)
                    candidates = self._title_candidates_from_response(raw, count)
                    for candidate in candidates:
                        self._insert_title_candidate(connection, project_id, keyword_id, candidate, source_type="ai", generation_job_id=job_id)
                    connection.execute("UPDATE title_generation_jobs SET status='succeeded',generated_count=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (len(candidates), job_id))
                row = connection.execute("SELECT * FROM title_generation_jobs WHERE id=?", (job_id,)).fetchone()
        except (sqlite3.Error, ValueError, TypeError, TitleGenerationProtocolError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error) or "Title generation failed."})
            return
        self._json(HTTPStatus.CREATED, self._title_job_payload(row))

    def _create_multi_provider_title_job(self, payload: Mapping[str, Any]) -> None:
        project_id, keyword_id = self._integer(payload, "project_id"), self._integer(payload, "keyword_id")
        locale = self._optional_text(payload, "locale") or "en-US"
        raw_titles = payload.get("competitor_titles", [])
        if project_id is None or keyword_id is None or not isinstance(raw_titles, list) or any(not isinstance(item, str) for item in raw_titles):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id, keyword_id and competitor_titles are required."})
            return
        references = [" ".join(item.split()) for item in raw_titles if item.strip()][:20]
        labels = {"openai": "ChatGPT", "gemini": "Gemini", "deepseek": "DeepSeek"}
        angles = {
            "openai": "Use a decision-framework or comparison angle for a US searcher. Do not reuse a headline structure from the references.",
            "gemini": "Use a practical use-case, local-service, or reader-scenario angle for a US searcher. Do not reuse another provider's angle.",
            "deepseek": "Use a cost, process, risk, or problem-solving angle for a US searcher. Do not reuse another provider's angle.",
        }
        failures: list[str] = []
        try:
            with self._database() as connection:
                keyword = self._title_keyword(connection, project_id, keyword_id, require_approved=True)
                intent = connection.execute("SELECT search_intent FROM keyword_reviews WHERE keyword_id=? ORDER BY id DESC LIMIT 1", (keyword_id,)).fetchone()
                request_data = {"keyword": keyword["keyword"], "locale": locale, "count": 3, "search_intent": intent[0] if intent else None, "title_type": self._optional_text(payload, "title_type"), "competitor_titles": references}
                with connection:
                    cursor = connection.execute("INSERT INTO title_generation_jobs(project_id,keyword_id,status,request_json,provider,requested_count,started_at) VALUES(?,?, 'running', ?,?,?, CURRENT_TIMESTAMP)", (project_id, keyword_id, json.dumps(request_data, ensure_ascii=False), "multi_provider", 9))
                    job_id = int(cursor.lastrowid)
                    generated = 0
                    seen_titles = {row[0] for row in connection.execute("SELECT normalized_title FROM keyword_title_candidates WHERE project_id=? AND keyword_id=? AND deleted_at IS NULL", (project_id, keyword_id)).fetchall()}
                    for provider, label in labels.items():
                        generator = getattr(self.server, "title_generators", {}).get(provider) if isinstance(getattr(self.server, "title_generators", {}), Mapping) else None
                        if generator is None:
                            configuration = _provider_configuration(self.server.ai_settings_path, provider)
                            generator = OpenAICompatibleTitleGenerator(*configuration) if configuration else None
                        if generator is None:
                            failures.append(f"{label} 未配置")
                            continue
                        try:
                            provider_request = {**request_data, "provider_generation_angle": angles[provider], "diversity_requirement": "Generate titles that differ in angle and structure from the Google references and other providers. Never copy a reference title."}
                            candidates = self._title_candidates_from_response(generator.generate(**provider_request), 3)
                            for candidate in candidates:
                                normalized = normalize_keyword(str(candidate["title"]))
                                if normalized in seen_titles:
                                    continue
                                seen_titles.add(normalized)
                                candidate["reason"] = f"[{label}] {candidate.get('reason') or '学习 Google 标题的搜索意图与结构，未复制原题。'}"
                                self._insert_title_candidate(connection, project_id, keyword_id, candidate, source_type="ai", generation_job_id=job_id)
                                generated += 1
                        except (TitleGenerationProtocolError, ValueError, TypeError, json.JSONDecodeError):
                            failures.append(f"{label} 生成失败")
                    connection.execute("UPDATE title_generation_jobs SET status=?,generated_count=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", ("succeeded" if generated else "failed", generated, job_id))
                row = connection.execute("SELECT * FROM title_generation_jobs WHERE id=?", (job_id,)).fetchone()
        except (sqlite3.Error, ValueError, TypeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error) or "Multi-provider title generation failed."})
            return
        response = self._title_job_payload(row)
        response["failures"] = failures
        self._json(HTTPStatus.CREATED, response)

    def _research_serp_titles(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        keyword_id = self._integer(payload, "keyword_id")
        locale = self._optional_text(payload, "locale") or "en-US"
        if project_id is None or keyword_id is None:
            return
        researcher = getattr(self.server.title_generator, "research_serp_titles", None)
        if not callable(researcher):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "请先为标题生成配置支持联网搜索的 AI。"})
            return
        try:
            with self._database() as connection:
                keyword = self._title_keyword(connection, project_id, keyword_id, require_approved=True)["keyword"]
            raw = researcher(keyword=keyword, locale=locale)
            titles, warning = self._serp_titles_from_response(raw)
        except (sqlite3.Error, ValueError, TypeError, TitleGenerationProtocolError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error) or "AI SERP 标题抓取失败。"})
            return
        response: dict[str, Any] = {"keyword": keyword, "titles": titles}
        if warning:
            response["warning"] = warning
        self._json(HTTPStatus.OK, response)

    def _research_browser_serp_titles(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        keyword_id = self._integer(payload, "keyword_id")
        locale = self._optional_text(payload, "locale") or "en-US"
        if project_id is None or keyword_id is None:
            return
        try:
            with self._database() as connection:
                keyword = self._title_keyword(connection, project_id, keyword_id, require_approved=True)["keyword"]
            titles = self.server.serp_title_client.fetch_titles(keyword=keyword, locale=locale, max_count=20)
        except GoogleSerpVerificationRequired as error:
            self._json(HTTPStatus.OK, {"keyword": keyword if "keyword" in locals() else "", "titles": [], "source_type": "browser", "verification_required": True, "verification_image": error.image_base64})
            return
        except (sqlite3.Error, ValueError, TypeError, GoogleSerpProtocolError) as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error) or "浏览器 Google 标题抓取失败。"})
            return
        self._json(HTTPStatus.OK, {"keyword": keyword, "titles": titles, "source_type": "browser"})

    def _create_manual_title_candidate(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        keyword_id = self._integer(payload, "keyword_id")
        title = self._text(payload, "title")
        if None in {project_id, keyword_id, title}:
            return
        candidate = {"title": title, "title_type": self._optional_text(payload, "title_type"), "search_intent": self._optional_text(payload, "search_intent"), "reason": self._optional_text(payload, "reason") or "Manually added title candidate."}
        try:
            with self._database() as connection:
                self._title_keyword(connection, project_id, keyword_id, require_approved=True)
                with connection:
                    candidate_id = self._insert_title_candidate(connection, project_id, keyword_id, candidate, source_type="manual")
                row = connection.execute("SELECT * FROM keyword_title_candidates WHERE id=?", (candidate_id,)).fetchone()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.CREATED, self._title_candidate_payload(row))

    def _list_title_candidates(self, keyword_id: int) -> None:
        values = parse_qs(urlsplit(self.path).query).get("project_id", [])
        if len(values) != 1:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id is required."})
            return
        try:
            project_id = int(values[0])
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id must be an integer."})
            return
        with self._database() as connection:
            self._title_keyword(connection, project_id, keyword_id, require_approved=False)
            rows = connection.execute("SELECT * FROM keyword_title_candidates WHERE project_id=? AND keyword_id=? AND deleted_at IS NULL ORDER BY CASE status WHEN 'selected' THEN 0 ELSE 1 END, quality_score DESC, id DESC", (project_id, keyword_id)).fetchall()
        candidates = [self._title_candidate_payload(row) for row in rows]
        self._json(HTTPStatus.OK, {"candidates": candidates, "selected_title": next((candidate for candidate in candidates if candidate["status"] == "selected"), None)})

    def _list_title_library(self) -> None:
        values = parse_qs(urlsplit(self.path).query).get("project_id", [])
        if len(values) != 1:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id is required."})
            return
        try:
            project_id = int(values[0])
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id must be an integer."})
            return
        with self._database() as connection:
            rows = connection.execute(
                """SELECT candidates.*,keywords.keyword,
                          assets.id AS content_asset_id, assets.current_outline_id,
                          assets.current_draft_id, assets.deleted_at AS content_asset_deleted_at
                   FROM keyword_title_candidates AS candidates
                   JOIN keywords ON keywords.id=candidates.keyword_id
                   LEFT JOIN content_assets AS assets ON assets.project_id=candidates.project_id
                       AND assets.selected_title_candidate_id=candidates.id AND assets.deleted_at IS NULL
                   WHERE candidates.project_id=? AND candidates.deleted_at IS NULL AND keywords.deleted_at IS NULL
                   ORDER BY CASE candidates.status WHEN 'selected' THEN 0 ELSE 1 END,candidates.created_at DESC,candidates.id DESC""",
                (project_id,),
            ).fetchall()
        self._json(HTTPStatus.OK, [self._title_candidate_payload(row) | self._workflow_status_payload(row) for row in rows])

    def _select_title_candidate(self, candidate_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        confirm_replace = payload.get("confirm_replace") is True
        try:
            with self._database() as connection:
                candidate = connection.execute("SELECT * FROM keyword_title_candidates WHERE id=? AND project_id=? AND deleted_at IS NULL", (candidate_id, project_id)).fetchone()
                if candidate is None:
                    raise ValueError("title candidate does not exist")
                existing = connection.execute("SELECT id FROM keyword_title_candidates WHERE keyword_id=? AND status='selected' AND deleted_at IS NULL", (candidate["keyword_id"],)).fetchone()
                if existing is not None and existing[0] != candidate_id and not confirm_replace:
                    self._json(HTTPStatus.CONFLICT, {"error": "A title is already selected. Confirm replace before selecting another."})
                    return
                with connection:
                    previous_id = int(existing[0]) if existing is not None and existing[0] != candidate_id else None
                    if previous_id is not None:
                        connection.execute("UPDATE keyword_title_candidates SET status='not_selected',selected_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (previous_id,))
                    connection.execute("UPDATE keyword_title_candidates SET status='selected',selected_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (candidate_id,))
                    action = "replaced" if previous_id is not None else "selected"
                    connection.execute("INSERT INTO keyword_title_selection_events(project_id,keyword_id,previous_candidate_id,selected_candidate_id,action,reason) VALUES(?,?,?,?,?,?)", (project_id, candidate["keyword_id"], previous_id, candidate_id, action, self._optional_text(payload, "reason")))
                row = connection.execute("SELECT * FROM keyword_title_candidates WHERE id=?", (candidate_id,)).fetchone()
        except sqlite3.Error as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, self._title_candidate_payload(row))

    def _delete_title_candidate(self, candidate_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        with self._database() as connection:
            row = connection.execute("SELECT status FROM keyword_title_candidates WHERE id=? AND project_id=? AND deleted_at IS NULL", (candidate_id, project_id)).fetchone()
            if row is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "title candidate does not exist"})
                return
            if row[0] == "selected":
                self._json(HTTPStatus.CONFLICT, {"error": "The selected title cannot be deleted. Replace it first."})
                return
            with connection:
                connection.execute("UPDATE keyword_title_candidates SET deleted_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (candidate_id,))
        self._json(HTTPStatus.OK, {"deleted": 1})

    def _delete_title_candidates(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        raw_ids = payload.get("candidate_ids", [])
        if project_id is None:
            return
        if not isinstance(raw_ids, list) or not raw_ids or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in raw_ids):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "candidate_ids must be a non-empty list of positive integers."})
            return
        candidate_ids = list(dict.fromkeys(raw_ids))
        marks = ",".join("?" for _ in candidate_ids)
        with self._database() as connection:
            rows = connection.execute(
                f"SELECT id,status FROM keyword_title_candidates WHERE project_id=? AND deleted_at IS NULL AND id IN ({marks})",
                (project_id, *candidate_ids),
            ).fetchall()
            if any(row["status"] == "selected" for row in rows):
                self._json(HTTPStatus.CONFLICT, {"error": "The selected title cannot be deleted. Replace it first."})
                return
            with connection:
                cursor = connection.execute(
                    f"UPDATE keyword_title_candidates SET deleted_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND deleted_at IS NULL AND id IN ({marks})",
                    (project_id, *candidate_ids),
                )
        self._json(HTTPStatus.OK, {"deleted": cursor.rowcount})

    def _delete_content_assets(self, payload: Mapping[str, Any], *, asset_id: int | None = None) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        if asset_id is not None:
            asset_ids = [asset_id]
        else:
            raw_ids = payload.get("content_asset_ids", [])
            if not isinstance(raw_ids, list) or not raw_ids or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in raw_ids):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "content_asset_ids must be a non-empty list of positive integers."})
                return
            asset_ids = list(dict.fromkeys(raw_ids))
        marks = ",".join("?" for _ in asset_ids)
        with self._database() as connection:
            with connection:
                cursor = connection.execute(
                    f"UPDATE content_assets SET deleted_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND deleted_at IS NULL AND id IN ({marks})",
                    (project_id, *asset_ids),
                )
        if cursor.rowcount == 0 and asset_id is not None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "content asset does not exist in this project"})
            return
        self._json(HTTPStatus.OK, {"deleted": cursor.rowcount})

    def _delete_keywords(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        clear_all = payload.get("clear_all") is True
        raw_ids = payload.get("keyword_ids", [])
        if clear_all:
            if payload.get("confirm_project_id") != project_id:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "confirm_project_id must match project_id before clearing."})
                return
        elif not isinstance(raw_ids, list) or not raw_ids or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in raw_ids):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "keyword_ids must be a non-empty list of positive integers."})
            return
        with self._database() as connection:
            if clear_all:
                cursor = connection.execute("UPDATE keywords SET deleted_at=CURRENT_TIMESTAMP WHERE project_id=? AND deleted_at IS NULL", (project_id,))
            else:
                marks = ",".join("?" for _ in raw_ids)
                cursor = connection.execute(f"UPDATE keywords SET deleted_at=CURRENT_TIMESTAMP WHERE project_id=? AND deleted_at IS NULL AND id IN ({marks})", (project_id, *raw_ids))
            connection.commit()
        self._json(HTTPStatus.OK, {"deleted": cursor.rowcount})

    def _import_keywords(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        filename = self._text(payload, "filename")
        csv_text = self._text(payload, "csv_text", allow_empty=True)
        country = self._text(payload, "country_code")
        language = self._text(payload, "language_code")
        metric_date = self._text(payload, "metric_date")
        if None in {project_id, filename, csv_text, country, language, metric_date}:
            return
        try:
            date.fromisoformat(metric_date)
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "metric_date must be YYYY-MM-DD."})
            return
        parsed = parse_keyword_csv(csv_text)
        source_type = self._source_type(payload)
        rows = self._preview_rows(parsed, source_type)
        preview = {"source_type": source_type, "country_code": country, "language_code": language, "rows": rows}
        try:
            with self._database() as connection:
                new_count, updated_count = self._change_counts(connection, project_id, rows, country, language)
                self._replace_same_day_snapshots(connection, project_id, rows, country, language, metric_date, source_type)
                result = KeywordImportService(connection).import_preview(project_id, preview, filename, metric_date)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.CREATED, {"accepted": result.accepted_rows, "new": new_count, "updated": updated_count, "rejected": result.rejected_rows})

    def _list_keywords(self) -> None:
        values = parse_qs(urlsplit(self.path).query).get("project_id", [])
        if len(values) != 1:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id is required."})
            return
        try:
            project_id = int(values[0])
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id must be an integer."})
            return
        with self._database() as connection:
            rows = connection.execute(
                """SELECT keywords.id,keywords.keyword,keywords.country_code,keywords.language_code,keywords.demand_estimate,
                   metrics.average_monthly_searches AS search_volume,metrics.competition_level,
                   metrics.competition_index,metrics.metric_date,
                   (SELECT categories.name FROM keyword_category_assignments AS assignments
                      JOIN keyword_categories AS categories ON categories.id=assignments.category_id
                      WHERE assignments.keyword_id=keywords.id ORDER BY assignments.created_at DESC LIMIT 1) AS category,
                   (SELECT reviews.search_intent FROM keyword_reviews AS reviews
                      WHERE reviews.keyword_id=keywords.id ORDER BY reviews.id DESC LIMIT 1) AS search_intent,
                   (SELECT reviews.is_seo_content_fit FROM keyword_reviews AS reviews
                      WHERE reviews.keyword_id=keywords.id ORDER BY reviews.id DESC LIMIT 1) AS is_seo_content_fit
                   ,(SELECT candidates.title FROM keyword_title_candidates AS candidates
                      WHERE candidates.keyword_id=keywords.id AND candidates.status='selected' AND candidates.deleted_at IS NULL LIMIT 1) AS selected_title
                   ,(SELECT COUNT(*) FROM keyword_title_candidates AS candidates
                      WHERE candidates.keyword_id=keywords.id AND candidates.deleted_at IS NULL) AS title_candidate_count
                   FROM keywords
                   LEFT JOIN keyword_metric_snapshots AS metrics ON metrics.id=(
                     SELECT latest.id FROM keyword_metric_snapshots AS latest WHERE latest.keyword_id=keywords.id
                     ORDER BY latest.metric_date DESC,latest.id DESC LIMIT 1)
                   WHERE keywords.project_id=? AND keywords.deleted_at IS NULL ORDER BY keywords.keyword COLLATE NOCASE,keywords.id""",
                (project_id,),
            ).fetchall()
        self._json(HTTPStatus.OK, [dict(row) for row in rows])

    @staticmethod
    def _keyword_title_candidates_path(path: str) -> int | None:
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "keywords"] or parts[3] != "title-candidates":
            return None
        try:
            return int(parts[2])
        except ValueError:
            return None

    @staticmethod
    def _title_candidate_path(path: str) -> int | None:
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "title-candidates"]:
            return None
        try:
            return int(parts[2])
        except ValueError:
            return None

    @staticmethod
    def _title_candidate_action_path(path: str, action: str) -> int | None:
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "title-candidates"] or parts[3] != action:
            return None
        try:
            return int(parts[2])
        except ValueError:
            return None

    @staticmethod
    def _content_asset_path(path: str) -> int | None:
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "content-assets"]: return None
        try: return int(parts[2])
        except ValueError: return None

    @staticmethod
    def _content_asset_action_path(path: str) -> tuple[int, str] | None:
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "content-assets"] or parts[3] not in {"briefs", "outlines", "generate", "generate-brief", "generate-outline", "generate-draft"}: return None
        try: return int(parts[2]), parts[3]
        except ValueError: return None

    @staticmethod
    def _content_asset(connection: sqlite3.Connection, project_id: int, asset_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT assets.*, keywords.keyword FROM content_assets assets JOIN keywords ON keywords.id=assets.keyword_id WHERE assets.id=? AND assets.project_id=? AND assets.deleted_at IS NULL", (asset_id, project_id)).fetchone()
        if row is None: raise ValueError("content asset does not exist in this project")
        return row

    @staticmethod
    def _workflow_status_payload(row: Mapping[str, Any]) -> dict[str, str]:
        outlined = row["current_outline_id"] is not None
        completed = row["current_draft_id"] is not None
        return {
            "outline_status": "completed" if outlined else "pending",
            "outline_status_label": "大纲完成" if outlined else "待大纲",
            "content_status": "completed" if completed else "pending",
            "content_status_label": "内容完成" if completed else "待生成内容",
        }

    @classmethod
    def _content_asset_payload(cls, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row) | cls._workflow_status_payload(row)

    def _content_asset_detail(self, connection: sqlite3.Connection, project_id: int, asset_id: int) -> dict[str, Any]:
        row = self._content_asset(connection, project_id, asset_id)
        payload = self._content_asset_payload(row)
        brief = connection.execute("SELECT * FROM content_briefs WHERE content_asset_id=? AND status='current' ORDER BY id DESC LIMIT 1", (asset_id,)).fetchone()
        outline = connection.execute("SELECT * FROM content_outlines WHERE content_asset_id=? ORDER BY id DESC LIMIT 1", (asset_id,)).fetchone()
        drafts = connection.execute("SELECT * FROM content_drafts WHERE content_asset_id=? ORDER BY version", (asset_id,)).fetchall()
        runs = connection.execute("SELECT * FROM content_generation_runs WHERE content_asset_id=? ORDER BY id", (asset_id,)).fetchall()
        jobs = connection.execute("SELECT * FROM content_generation_jobs WHERE content_asset_id=? ORDER BY id", (asset_id,)).fetchall()
        current_draft = connection.execute("SELECT * FROM content_drafts WHERE id=?", (row["current_draft_id"],)).fetchone() if row["current_draft_id"] is not None else None
        payload["brief"] = self._content_brief_payload(brief) if brief else None
        payload["outline"] = self._content_outline_payload(connection, outline) if outline else None
        payload["drafts"] = [self._content_draft_payload(draft) for draft in drafts]
        payload["current_draft"] = self._content_draft_payload(current_draft) if current_draft else None
        payload["generation_runs"] = [self._content_run_payload(run) for run in runs]
        payload["generation_jobs"] = [dict(job) for job in jobs]
        # Kept as a compact compatibility alias for the first content-system UI.
        payload["runs"] = payload["generation_runs"]
        return payload

    @staticmethod
    def _content_brief_payload(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row); value["sources"] = json.loads(value.pop("sources_json") or "[]"); value["brief"] = json.loads(value.pop("brief_json") or "{}"); return value

    @staticmethod
    def _content_outline_payload(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        sections: list[dict[str, Any]] = []
        for item in connection.execute("SELECT * FROM content_outline_sections WHERE outline_id=? ORDER BY position,id", (row["id"],)).fetchall():
            section = dict(item)
            extra = json.loads(section.pop("section_json", "{}") or "{}")
            if isinstance(extra, Mapping):
                section.update(extra)
            sections.append(section)
        value["sections"] = sections
        return value

    @staticmethod
    def _content_draft_payload(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["sources_used"] = json.loads(value.pop("sources_used_json") or "[]")
        value["unresolved_verify"] = json.loads(value.pop("unresolved_verify_json") or "[]")
        value["qa"] = json.loads(value.pop("qa_json") or "{}")
        return value

    @staticmethod
    def _content_run_payload(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["input"] = json.loads(value.pop("input_json") or "{}")
        raw_output = value.pop("output_json")
        value["output"] = json.loads(raw_output) if raw_output else None
        return value

    @staticmethod
    def _title_candidates_from_response(raw: Any, requested_count: int) -> list[dict[str, str | bool | None]]:
        value = KeywordDiscoveryRequestHandler._ai_json_object(raw)
        if not isinstance(value, Mapping) or not isinstance(value.get("candidates"), list):
            raise ValueError("AI title generator must return a JSON object with candidates.")
        candidates: list[dict[str, str | bool | None]] = []
        seen: set[str] = set()
        for item in value["candidates"]:
            if not isinstance(item, Mapping):
                continue
            title = item.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            title = " ".join(title.split())
            normalized = normalize_keyword(title)
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append({
                "title": title,
                "title_type": item.get("title_type") if isinstance(item.get("title_type"), str) else None,
                "search_intent": item.get("search_intent") if isinstance(item.get("search_intent"), str) else None,
                "reason": item.get("reason") if isinstance(item.get("reason"), str) else "AI-generated SEO title candidate.",
                "primary_keyword_included": item.get("primary_keyword_included") is True,
            })
            if len(candidates) >= requested_count:
                break
        if not candidates:
            raise ValueError("AI title generator returned no valid title candidates.")
        return candidates

    @staticmethod
    def _serp_titles_from_response(raw: Any) -> tuple[list[dict[str, str | int | None]], str | None]:
        if isinstance(raw, str) and not raw.strip():
            return [], "AI 未返回可用内容；请改用支持联网搜索的模型或配置 SERP 数据服务。"
        value = KeywordDiscoveryRequestHandler._ai_json_object(raw)
        if not isinstance(value, Mapping) or not isinstance(value.get("titles"), list):
            raise ValueError("AI SERP research must return a JSON object with titles.")
        titles: list[dict[str, str | int | None]] = []
        seen: set[str] = set()
        for index, item in enumerate(value["titles"]):
            if not isinstance(item, Mapping) or not isinstance(item.get("title"), str):
                continue
            title = " ".join(item["title"].split())
            normalized = normalize_keyword(title)
            if not title or normalized in seen:
                continue
            seen.add(normalized)
            rank = item.get("rank")
            titles.append({
                "rank": rank if isinstance(rank, int) and rank > 0 else len(titles) + 1,
                "title": title,
                "source": item.get("source") if isinstance(item.get("source"), str) and item.get("source").strip() else None,
            })
            if len(titles) >= 20:
                break
        warning = value.get("warning") if isinstance(value.get("warning"), str) and value.get("warning").strip() else None
        return titles, warning

    @staticmethod
    def _ai_json_object(raw: Any) -> Mapping[str, Any]:
        if isinstance(raw, Mapping):
            return raw
        if not isinstance(raw, str):
            raise ValueError("AI response must be a JSON object.")
        text = raw.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            text = fenced.group(1)
        elif not text.startswith("{"):
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
        value = json.loads(text)
        if not isinstance(value, Mapping):
            raise ValueError("AI response must contain a JSON object.")
        return value

    def _insert_title_candidate(self, connection: sqlite3.Connection, project_id: int, keyword_id: int, candidate: Mapping[str, Any], *, source_type: str, generation_job_id: int | None = None) -> int:
        title = str(candidate["title"]).strip()
        keyword = self._title_keyword(connection, project_id, keyword_id, require_approved=False)["keyword"]
        normalized = normalize_keyword(title)
        keyword_normalized = normalize_keyword(keyword)
        included = keyword_normalized in normalized
        length_ok = 18 <= len(title) <= 75
        score = (35 if included else 0) + (20 if length_ok else 8) + 25 + 10 + 10
        details = {"keyword_coverage": included, "length_ok": length_ok, "locale": "en-US", "score_rule": "seo_title_us_v1"}
        cursor = connection.execute(
            """INSERT INTO keyword_title_candidates(project_id,keyword_id,generation_job_id,title,normalized_title,title_type,search_intent,reason,source_type,quality_score,quality_details_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, keyword_id, generation_job_id, title, normalized, candidate.get("title_type"), candidate.get("search_intent"), candidate.get("reason"), source_type, score, json.dumps(details, ensure_ascii=False)),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _title_candidate_payload(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["quality_details"] = json.loads(payload.pop("quality_details_json") or "{}")
        return payload

    @staticmethod
    def _title_job_payload(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["request"] = json.loads(payload.pop("request_json") or "{}")
        return payload

    @staticmethod
    def _title_keyword(connection: sqlite3.Connection, project_id: int, keyword_id: int, *, require_approved: bool) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM keywords WHERE id=? AND project_id=? AND deleted_at IS NULL", (keyword_id, project_id)).fetchone()
        if row is None:
            raise ValueError("keyword does not exist in this project")
        if require_approved and row["status"] != "approved":
            raise ValueError("only an approved keyword can generate or receive titles")
        return row

    def _upsert_expanded_keyword(self, connection: sqlite3.Connection, project_id: int, keyword: str, country: str, language: str, seed_keyword: str, item: Mapping[str, Any]) -> tuple[int, bool]:
        normalized = normalize_keyword(keyword)
        row = connection.execute(
            "SELECT id FROM keywords WHERE project_id=? AND normalized_keyword=? AND country_code=? AND language_code=?",
            (project_id, normalized, country, language),
        ).fetchone()
        created = row is None
        demand_estimate = self._demand_estimate(item.get("demand_estimate"))
        if row is None:
            cursor = connection.execute(
                """INSERT INTO keywords(project_id,keyword,normalized_keyword,country_code,language_code,status,demand_estimate)
                   VALUES(?,?,?,?,?,?,?)""",
                (project_id, keyword, normalized, country, language, "approved" if item.get("is_seo_content_fit") is True else "pending_review", demand_estimate),
            )
            keyword_id = int(cursor.lastrowid)
        else:
            keyword_id = int(row[0])
            connection.execute("UPDATE keywords SET deleted_at=NULL,demand_estimate=COALESCE(?, demand_estimate),last_seen_at=CURRENT_TIMESTAMP WHERE id=?", (demand_estimate, keyword_id))
        connection.execute(
            "INSERT INTO keyword_sources(keyword_id,source_type,seed_keyword) VALUES(?,?,?)",
            (keyword_id, "google_suggest", seed_keyword),
        )
        self._assign_category(connection, project_id, keyword_id, item)
        self._record_keyword_review(connection, keyword_id, seed_keyword, item)
        return keyword_id, created

    @staticmethod
    def _demand_estimate(value: Any) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
            raise ValueError("demand_estimate must be an integer from 0 to 100")
        return value

    def _assign_category(self, connection: sqlite3.Connection, project_id: int, keyword_id: int, item: Mapping[str, Any]) -> None:
        category = item.get("category")
        if not isinstance(category, str) or not category.strip():
            return
        name = " ".join(category.split())
        normalized = normalize_keyword(name)
        connection.execute("INSERT OR IGNORE INTO keyword_categories(project_id,name,normalized_name) VALUES(?,?,?)", (project_id, name, normalized))
        category_id = connection.execute("SELECT id FROM keyword_categories WHERE project_id=? AND normalized_name=?", (project_id, normalized)).fetchone()[0]
        connection.execute("INSERT OR REPLACE INTO keyword_category_assignments(keyword_id,category_id,source,confidence) VALUES(?,?,?,?)", (keyword_id, category_id, "ai" if item.get("review_provider") == "ai" else "rule", self._review_confidence(item.get("review_confidence"))))

    def _record_keyword_review(self, connection: sqlite3.Connection, keyword_id: int, seed_keyword: str, item: Mapping[str, Any]) -> None:
        is_fit = item.get("is_seo_content_fit")
        same_topic = item.get("same_topic_as_seed")
        if not isinstance(is_fit, bool) or not isinstance(same_topic, bool):
            return
        intent = item.get("search_intent") if isinstance(item.get("search_intent"), str) else None
        action = item.get("recommended_action") if isinstance(item.get("recommended_action"), str) else None
        reason = item.get("review_reason") if isinstance(item.get("review_reason"), str) else None
        connection.execute(
            """INSERT INTO keyword_reviews(keyword_id,seed_keyword,provider,is_seo_content_fit,same_topic_as_seed,search_intent,recommended_action,reason,confidence)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (keyword_id, seed_keyword, "ai" if item.get("review_provider") == "ai" else "rule", int(is_fit), int(same_topic), intent, action, reason, self._review_confidence(item.get("review_confidence"))),
        )

    @staticmethod
    def _review_confidence(value: Any) -> float | None:
        if value is None:
            return None
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 1:
            raise ValueError("review_confidence must be a number from 0 to 1")
        return float(value)

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        connection = initialize_database(self.server.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _preview_rows(parsed: Any, source_type: str) -> list[dict[str, Any]]:
        rows = []
        for number, record in enumerate(parsed.records, start=2):
            rows.append({"row_number": number, "keyword": record.keyword, "average_monthly_searches": record.avg_monthly_searches, "competition_level": record.competition, "competition_index": record.competition_index, "low_top_of_page_bid_micros": KeywordDiscoveryRequestHandler._micros(record.top_of_page_bid_low), "high_top_of_page_bid_micros": KeywordDiscoveryRequestHandler._micros(record.top_of_page_bid_high), "errors": [], "source_type": source_type})
        for error in parsed.errors:
            rows.append({"row_number": error.row_number, "keyword": "", "errors": [error.message], "source_type": source_type})
        return rows

    @staticmethod
    def _change_counts(connection: sqlite3.Connection, project_id: int, rows: list[dict[str, Any]], country: str, language: str) -> tuple[int, int]:
        existing = {str(row[0]) for row in connection.execute("SELECT normalized_keyword FROM keywords WHERE project_id=? AND country_code=? AND language_code=?", (project_id, country, language))}
        new_count = updated_count = 0
        seen: set[str] = set()
        for row in rows:
            if row.get("errors") or not str(row.get("keyword") or "").strip():
                continue
            term = normalize_keyword(str(row["keyword"]))
            if term in existing or term in seen: updated_count += 1
            else: new_count += 1
            seen.add(term)
        return new_count, updated_count

    @staticmethod
    def _replace_same_day_snapshots(connection: sqlite3.Connection, project_id: int, rows: list[dict[str, Any]], country: str, language: str, metric_date: str, source_type: str) -> None:
        terms = sorted({normalize_keyword(str(row["keyword"])) for row in rows if not row.get("errors") and str(row.get("keyword") or "").strip()})
        if not terms: return
        marks = ",".join("?" for _ in terms)
        ids = [row[0] for row in connection.execute(f"SELECT id FROM keywords WHERE project_id=? AND country_code=? AND language_code=? AND normalized_keyword IN ({marks})", (project_id, country, language, *terms))]
        if ids:
            marks = ",".join("?" for _ in ids)
            connection.execute(f"DELETE FROM keyword_metric_snapshots WHERE keyword_id IN ({marks}) AND source_type=? AND metric_date=? AND country_code=? AND language_code=?", (*ids, source_type, metric_date, country, language))

    def _read_json(self) -> Mapping[str, Any] | None:
        try:
            value = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be JSON."}); return None
        if not isinstance(value, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON body must be an object."}); return None
        return value

    def _text(self, payload: Mapping[str, Any], field: str, *, allow_empty: bool = False) -> str | None:
        value = payload.get(field)
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"{field} is required."}); return None
        return value if allow_empty else value.strip()

    @staticmethod
    def _optional_text(payload: Mapping[str, Any], field: str) -> str | None:
        value = payload.get(field); return value.strip() if isinstance(value, str) and value.strip() else None

    def _integer(self, payload: Mapping[str, Any], field: str) -> int | None:
        try: value = int(payload.get(field))
        except (TypeError, ValueError): self._json(HTTPStatus.BAD_REQUEST, {"error": f"{field} must be an integer."}); return None
        if value < 1: self._json(HTTPStatus.BAD_REQUEST, {"error": f"{field} must be positive."}); return None
        return value

    @staticmethod
    def _score_keyword(item: Mapping[str, Any]) -> str:
        value = item.get("keyword")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("keyword is required.")
        return value.strip()

    @staticmethod
    def _score_number(item: Mapping[str, Any], field: str) -> float:
        value = item.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{field} must be a number.")
        return float(value)

    @staticmethod
    def _score_integer(item: Mapping[str, Any], field: str) -> int:
        value = item.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{field} must be an integer.")
        return value

    def _limit(self, payload: Mapping[str, Any], field: str, default: int) -> int:
        value = payload.get(field, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{field} must be a positive integer")
        return value

    @staticmethod
    def _source_type(payload: Mapping[str, Any]) -> str:
        source = str(payload.get("source_type") or "file_import")
        return source if source in {"file_import", "google_ads"} else "file_import"

    @staticmethod
    def _micros(value: float | None) -> int | None: return None if value is None else int(value * 1_000_000)

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content)


def create_server(host: str = "127.0.0.1", port: int = 0, database_path: str | Path = ":memory:", suggest_client: Any | None = None, keyword_reviewer: Any | None = None, title_generator: Any | None = None, serp_title_client: Any | None = None, ai_settings_path: Path | None = None, content_generator: Any | None = None) -> KeywordDiscoveryServer:
    server = KeywordDiscoveryServer((host, port), KeywordDiscoveryRequestHandler)
    server.database_path = database_path
    server.ai_settings_path = ai_settings_path or AI_SETTINGS_FILE
    server.allow_environment_ai_fallback = ai_settings_path is None
    server.suggest_client = suggest_client or GoogleSuggestClient()
    server.keyword_reviewer = keyword_reviewer or _default_keyword_reviewer(server.ai_settings_path)
    server.title_generator = title_generator or _default_title_generator(server.ai_settings_path)
    # None means resolve the current saved content-generation provider per request.
    server.content_generator = content_generator
    server.serp_title_client = serp_title_client or BrowserSerpTitleClient()
    return server


def _default_keyword_reviewer(ai_settings_path: Path = AI_SETTINGS_FILE) -> Any:
    configuration = _ai_configuration(ai_settings_path, purpose="keyword_review")
    if configuration is not None:
        api_key, base_url, model = configuration
        return OpenAICompatibleKeywordReviewer(api_key, base_url, model)
    return RuleBasedKeywordReviewer()


def _default_title_generator(ai_settings_path: Path = AI_SETTINGS_FILE) -> Any:
    configuration = _ai_configuration(ai_settings_path, purpose="title_generation")
    if configuration is not None:
        api_key, base_url, model = configuration
        return OpenAICompatibleTitleGenerator(api_key, base_url, model)
    return RuleBasedTitleGenerator()


def _ai_configuration(ai_settings_path: Path = AI_SETTINGS_FILE, *, purpose: str = "keyword_review") -> tuple[str, str, str] | None:
    provider = _ai_assignments(ai_settings_path).get(purpose, "openai")
    return _provider_configuration(ai_settings_path, provider)


def _provider_configuration(ai_settings_path: Path, provider: str | None) -> tuple[str, str, str] | None:
    if provider not in AI_PROVIDERS:
        return None
    document = _ai_settings_document(ai_settings_path)
    raw_profiles = document.get("providers")
    raw = raw_profiles.get(provider) if isinstance(raw_profiles, Mapping) else None
    if isinstance(raw, Mapping):
        api_key, base_url, model = raw.get("api_key"), raw.get("base_url"), raw.get("model")
        if all(isinstance(item, str) and item.strip() for item in (api_key, base_url, model)):
            return api_key.strip(), _normalize_ai_base_url(base_url), model.strip()
    if document.get("provider") == provider:
        saved = _saved_ai_settings(ai_settings_path)
        if saved is not None:
            return saved
    if provider == "openai":
        api_key = _configured_value("SEO_AI_API_KEY") or _configured_value("Chatgpt_API_KEY")
        base_url = _configured_value("SEO_AI_BASE_URL") or _configured_value("Chatgpt_BASE_URL")
        model = _configured_value("SEO_AI_MODEL") or _configured_value("Chatgpt_MODEL") or "gpt-5.5"
        return (api_key, _normalize_ai_base_url(base_url), model) if api_key and base_url else None
    if provider == "gemini":
        api_key = _configured_value("GEMINI_API_KEY")
        base_url = _configured_value("GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta/openai"
        model = _configured_value("GEMINI_MODEL") or "gemini-2.5-flash"
        return (api_key, _normalize_ai_base_url(base_url), model) if api_key else None
    api_key = _configured_value("DEEPSEEK_API_KEY")
    base_url = _configured_value("DEEPSEEK_BASE_URL")
    model = _configured_value("DEEPSEEK_MODEL") or "deepseek-v4-pro"
    return (api_key, _normalize_ai_base_url(base_url), model) if api_key and base_url else None


def _ai_settings_document(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _ai_assignments(path: Path) -> dict[str, str]:
    document = _ai_settings_document(path)
    assignments = _normalize_ai_assignments(document.get("assignments"))
    if assignments is not None:
        return assignments
    legacy_provider = document.get("provider")
    if legacy_provider in AI_PROVIDERS:
        return {"keyword_review": legacy_provider, "title_generation": legacy_provider}
    return dict(DEFAULT_AI_ASSIGNMENTS)


def _normalize_ai_assignments(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    result = {key: value.get(key, default) for key, default in DEFAULT_AI_ASSIGNMENTS.items()}
    if any(provider not in AI_PROVIDERS for provider in result.values()):
        return None
    return {key: str(provider) for key, provider in result.items()}


def _public_ai_profiles(path: Path) -> dict[str, dict[str, object]]:
    profiles: dict[str, dict[str, object]] = {}
    for provider in AI_PROVIDERS:
        configuration = _provider_configuration(path, provider)
        profiles[provider] = {"configured": configuration is not None, "base_url": configuration[1] if configuration else None, "model": configuration[2] if configuration else None}
    return profiles


def _saved_ai_settings(path: Path) -> tuple[str, str, str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    api_key, base_url, model = value.get("api_key"), value.get("base_url"), value.get("model")
    if all(isinstance(item, str) and item.strip() for item in (api_key, base_url, model)):
        return api_key.strip(), _normalize_ai_base_url(base_url), model.strip()
    return None


def _normalize_ai_base_url(value: str) -> str:
    """Accept either an API base URL or a copied chat-completions endpoint."""
    normalized = value.strip().rstrip("/")
    suffix = "/chat/completions"
    if normalized.casefold().endswith(suffix):
        return normalized[: -len(suffix)]
    return normalized


def _saved_ai_provider(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    provider = value.get("provider") if isinstance(value, Mapping) else None
    return provider.strip() if isinstance(provider, str) and provider.strip() else None


def _provider_for_base_url(base_url: str) -> str:
    value = base_url.casefold()
    if "generativelanguage.googleapis.com" in value:
        return "gemini"
    if "deepseek" in value:
        return "deepseek"
    if "openai.com" in value:
        return "openai"
    return "openai_compatible"


def _configured_value(name: str) -> str | None:
    """Read a permitted local PowerShell environment assignment without executing it."""

    environment_value = os.getenv(name)
    if environment_value:
        return environment_value
    try:
        content = LOCAL_CONFIGURATION_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(rf"^\s*\$env:{re.escape(name)}\s*=\s*(['\"])(.*?)\1\s*$", content, flags=re.MULTILINE)
    return match.group(2).strip() if match and match.group(2).strip() else None
