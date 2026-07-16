"""Local static workspace with CSV-import and Google Suggest APIs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import json
import os
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping
from urllib.parse import parse_qs, urlsplit

from seo_control.application.csv_keyword_import import parse_keyword_csv
from seo_control.application.ai_keyword_reviewer import OpenAICompatibleKeywordReviewer, RuleBasedKeywordReviewer
from seo_control.application.google_suggest_client import GoogleSuggestClient, GoogleSuggestProtocolError
from seo_control.application.keyword_expansion_service import KeywordExpansionService
from seo_control.application.keyword_import_service import KeywordImportService
from seo_control.domain.keywords import normalize_keyword
from seo_control.domain.keyword_scoring import KeywordScoringInput, calculate_keyword_score
from seo_control.infrastructure.database import initialize_database


WEB_ROOT = Path(__file__).resolve().parents[2] / "web"


class KeywordDiscoveryServer(ThreadingHTTPServer):
    database_path: str | Path
    suggest_client: Any
    keyword_reviewer: Any | None


class KeywordDiscoveryRequestHandler(SimpleHTTPRequestHandler):
    server: KeywordDiscoveryServer

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/keywords":
            self._list_keywords()
        elif path in {"", "/"}:
            self._serve_index()
        else:
            super().do_GET()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/api/projects", "/api/keyword-imports", "/api/suggest-expansions", "/api/keyword-opportunity-scores", "/api/expanded-keywords", "/api/ai-keyword-reviews"}:
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
        else:
            self._expand_suggestions(payload)

    def do_DELETE(self) -> None:
        if urlsplit(self.path).path != "/api/keywords":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        payload = self._read_json()
        if payload is not None:
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
        reviewer = self.server.keyword_reviewer
        if reviewer is None:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "AI keyword reviewer is not configured."})
            return
        try:
            review = reviewer.review(seed_keyword=seed_keyword, keyword=keyword, language=language)
            if hasattr(review, "as_dict"):
                review = review.as_dict()
            if not isinstance(review, Mapping):
                raise ValueError("AI reviewer must return an object.")
        except Exception as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {"review": dict(review)})

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
                   FROM keywords
                   LEFT JOIN keyword_metric_snapshots AS metrics ON metrics.id=(
                     SELECT latest.id FROM keyword_metric_snapshots AS latest WHERE latest.keyword_id=keywords.id
                     ORDER BY latest.metric_date DESC,latest.id DESC LIMIT 1)
                   WHERE keywords.project_id=? AND keywords.deleted_at IS NULL ORDER BY keywords.keyword COLLATE NOCASE,keywords.id""",
                (project_id,),
            ).fetchall()
        self._json(HTTPStatus.OK, [dict(row) for row in rows])

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


def create_server(host: str = "127.0.0.1", port: int = 0, database_path: str | Path = ":memory:", suggest_client: Any | None = None, keyword_reviewer: Any | None = None) -> KeywordDiscoveryServer:
    server = KeywordDiscoveryServer((host, port), KeywordDiscoveryRequestHandler)
    server.database_path = database_path
    server.suggest_client = suggest_client or GoogleSuggestClient()
    server.keyword_reviewer = keyword_reviewer or _default_keyword_reviewer()
    return server


def _default_keyword_reviewer() -> Any:
    api_key = os.getenv("SEO_AI_API_KEY")
    base_url = os.getenv("SEO_AI_BASE_URL")
    model = os.getenv("SEO_AI_MODEL")
    if api_key and base_url and model:
        return OpenAICompatibleKeywordReviewer(api_key, base_url, model)
    return RuleBasedKeywordReviewer()
