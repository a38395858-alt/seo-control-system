"""Local static workspace with CSV-import and Google Suggest APIs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
import base64
import csv
import html
from html.parser import HTMLParser
import io
import json
import mimetypes
import math
import os
import re
import hashlib
import hmac
import time
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping
from urllib.error import HTTPError
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import requests
from PIL import Image, ImageOps
try:
    import win32crypt
except ImportError:  # pragma: no cover - Windows desktop runtime supplies this
    win32crypt = None

from seo_control.application.csv_keyword_import import parse_keyword_csv
from seo_control.application.ai_keyword_reviewer import OpenAICompatibleKeywordReviewer, RuleBasedKeywordReviewer
from seo_control.application.ai_title_generator import OpenAICompatibleTitleGenerator, RuleBasedTitleGenerator, TitleGenerationProtocolError
from seo_control.application.content_generator import (
    ContentGenerationProtocolError,
    FIXED_CONTENT_SAFETY_RULES,
    OpenAICompatibleContentGenerator,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    _stage_instruction,
)
from seo_control.application.browser_serp_title_client import BrowserSerpTitleClient, GoogleSerpProtocolError, GoogleSerpVerificationRequired
from seo_control.application.browser_competitor_content_client import BrowserCompetitorContentClient, CompetitorContentProtocolError
from seo_control.application.serper_search_client import SerperSearchClient, SerperSearchProtocolError
from seo_control.application.google_suggest_client import GoogleSuggestClient, GoogleSuggestProtocolError
from seo_control.application.gsc_browser_capture_client import GscBrowserCaptureClient, GscBrowserCaptureError
from seo_control.application.gsc_feedback import GscFeedbackService
from seo_control.application.keyword_expansion_service import KeywordExpansionService
from seo_control.application.keyword_import_service import KeywordImportService
from seo_control.application.agent_tools import AgentToolService
from seo_control.application.agent_workflows import AgentWorkflowInputError, run_content_workflow, run_content_workflow_skeleton
from seo_control.application.competitor_intelligence import CollectionService, normalize_collection_url
from seo_control.application.memory_learning import MemoryLearningService
from seo_control.domain.keywords import normalize_keyword
from seo_control.domain.keyword_scoring import KeywordScoringInput, calculate_keyword_score
from seo_control.infrastructure.database import initialize_database
from seo_control.infrastructure.credential_vault import CredentialVault
from seo_control.infrastructure.runtime_database import RuntimeDatabaseController, RuntimeDatabaseMode
from seo_control.infrastructure.runtime_postgres import (
    PostgresRuntimeCollectionRepository,
    PostgresRuntimeConnectionFactory,
    PostgresRuntimeKeywordRepository,
    PostgresRuntimeProjectRepository,
)
from seo_control.infrastructure.repositories import (
    CollectionRepository,
    KeywordRepository,
    ProjectRepository,
    ShadowCollectionRepository,
    ShadowKeywordRepository,
    ShadowProjectRepository,
    SQLiteCollectionRepository,
    SQLiteKeywordRepository,
    SQLiteProjectRepository,
)
from seo_control.infrastructure.task_queue import DurableTaskQueue
from platform_api.site_crawler import crawl_site


WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
LOCAL_CONFIGURATION_FILE = Path(__file__).resolve().parents[2] / "配置文件.txt"
AI_SETTINGS_FILE = Path(__file__).resolve().parents[2] / "data" / "ai-settings.json"
AI_PROVIDERS = ("openai", "gemini", "deepseek")
IMAGE_GENERATION_PROVIDERS = ("openai", "siliconflow")
SILICONFLOW_IMAGE_BASE_URL = "https://api.siliconflow.cn/v1"
CONTENT_SECTION_IMAGE_SIZE = "800x600"
CONTENT_SECTION_IMAGE_DIMENSIONS = (800, 600)
# Free-tier image generation is rate-limited more strictly than the request
# API advertises. Twenty seconds keeps a full H2 batch below the observed
# burst threshold while still letting the user finish it in one operation.
SILICONFLOW_IMAGE_REQUEST_INTERVAL_SECONDS = 20
DEFAULT_AI_ASSIGNMENTS = {"keyword_review": "openai", "title_generation": "openai", "content_generation": "openai"}
CONTENT_PROVIDER_LABELS = {"openai": "ChatGPT", "gemini": "Gemini", "deepseek": "DeepSeek"}
AUTHORITY_SEARCH_FILE_EXCLUSIONS = "-filetype:pdf -filetype:doc -filetype:docx -filetype:xls -filetype:xlsx -filetype:ppt -filetype:pptx -filetype:csv -filetype:zip"
AUTHORITY_NON_ARTICLE_PATH_MARKERS = ("/documentcenter/", "/docview", "/pdfjsviewer/", "/virtual-library/", "/weblink/", "/records/", "/download/", "/bidopportunities/")
# Social platforms are discovery noise rather than reusable editorial sources.
# Keep them visible only as skipped search results; never crawl or learn from them.
COMPETITOR_NON_ARTICLE_DOMAINS = (
    "reddit.com", "pinterest.com", "youtube.com", "youtu.be", "instagram.com",
    "facebook.com", "tiktok.com", "twitter.com", "x.com", "linkedin.com",
    "threads.net", "snapchat.com", "tumblr.com", "quora.com",
)
COMPETITOR_MARKETPLACE_DOMAINS = ("amazon.com", "ebay.com", "aliexpress.com", "temu.com", "walmart.com", "etsy.com", "wayfair.com")
COMPETITOR_PRODUCT_PATH_MARKERS = ("/product/", "/products/", "/collections/", "/category/", "/categories/", "/shop/", "/store/", "/dp/", "/best-sellers/")
COMPETITOR_TRACKING_QUERY_PARAMETERS = frozenset({
    "dclid", "fbclid", "gclid", "gbraid", "mc_cid", "mc_eid", "msclkid", "rsltid", "srsltid", "ttclid", "twclid", "wbraid",
})


def normalize_competitor_url(value: str) -> str:
    """Return a stable competitor-page key without search/ad tracking noise."""
    return normalize_collection_url(value)


def _looks_like_image_bytes(payload: bytes) -> bool:
    """Accept image downloads whose CDN omits a useful Content-Type header."""
    return payload.startswith((
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"GIF87a",
        b"GIF89a",
    )) or (payload.startswith(b"RIFF") and payload[8:12] == b"WEBP")


def _image_retry_delay(error: Exception, attempt: int, provider: str) -> int:
    """Honor provider throttling instead of immediately exhausting free quotas."""
    if isinstance(error, HTTPError) and error.code == HTTPStatus.TOO_MANY_REQUESTS:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        if isinstance(retry_after, str) and retry_after.isdigit():
            return min(max(int(retry_after), SILICONFLOW_IMAGE_REQUEST_INTERVAL_SECONDS), 90)
        return min(SILICONFLOW_IMAGE_REQUEST_INTERVAL_SECONDS * (2 ** (attempt - 1)), 90)
    return (SILICONFLOW_IMAGE_REQUEST_INTERVAL_SECONDS if provider == "siliconflow" else 2) * attempt


def competitor_search_queries(title: str, keyword: str) -> list[str]:
    """Return a full-intent query plus a compact research variant.

    Product-name titles begin with their concise keyword, because their full
    marketing headline usually returns retail listings. Informational titles
    (including comparisons, guides and inspiration/idea lists) begin with the
    exact approved title, because stripping their intent also strips the
    article-shaped results needed for competitor research.
    """
    original = " ".join(title.split())
    core_keyword = " ".join(keyword.split())
    if not original and not core_keyword:
        return []
    stop_words = {"a", "an", "and", "are", "can", "do", "does", "for", "how", "is", "look", "of", "or", "the", "to", "what", "when", "which", "with"}
    raw_terms = re.findall(r"[A-Za-z0-9]+", f"{title} {keyword}")
    terms: list[str] = []
    seen: set[str] = set()
    for term in raw_terms:
        lowered = term.casefold()
        if len(lowered) < 2 or lowered in stop_words or lowered in seen:
            continue
        seen.add(lowered)
        terms.append(term)
    if not terms:
        return [core_keyword or original]
    compact = " ".join(terms)
    title_intent = original.casefold()
    inspiration_intent = any(
        marker in title_intent
        for marker in (" idea", "inspiration", "inspiring", "tips", "examples", "designs", "styles")
    )
    benefit_or_design_intent = any(
        marker in title_intent
        for marker in (" benefit", "benefits", "advantage", "advantages", " design", "planning", "layout")
    )
    if inspiration_intent:
        # A literal headline such as "10 Inspiring Ideas ..." frequently
        # returns Pinterest and video cards. Keep its subject keyword but use
        # a guide-shaped variant to surface editorial pages on the second
        # query; the exact title remains the first query.
        compact = core_keyword or compact
        if "idea" not in compact.casefold():
            compact = f"{compact} ideas"
        if "guide" not in compact.casefold():
            compact = f"{compact} guide"
    elif benefit_or_design_intent:
        # Benefits and design headlines are informational, yet their raw
        # product keyword often produces shopping SERPs.  The second query
        # must explicitly ask Google for a guide/article-shaped result set.
        compact = core_keyword or compact
        suffix = "design guide" if any(marker in title_intent for marker in (" design", "planning", "layout")) else "benefits guide"
        if suffix not in compact.casefold():
            compact = f"{compact} {suffix}"
    elif any(marker in title_intent for marker in ("waterproof", "water resistant", "weatherproof")):
        compact = f"{compact} IP rating"
    elif any(marker in title_intent for marker in ("how ", "what to", "guide", "look for", "best ", "compare", "vs.")):
        compact = f"{compact} guide"
    # An informational headline carries intent that the raw product keyword
    # loses (for example, "solar vs wired" or "inspiring ideas"). For ordinary
    # product-style titles, begin with the concise keyword and use the
    # title-derived form as a fallback.
    intent_first = any(
        marker in title_intent
        for marker in (
            " vs ", " vs. ", " versus ", "compare", "comparison",
            "how ", "what to", "look for", "guide", "review",
            " idea", "inspiration", "inspiring", "tips", "examples",
            "design", "designs", "styles", "ways to", "benefit", "advantages",
        )
    )
    primary = original if intent_first else (core_keyword or original)
    return list(dict.fromkeys(query for query in (primary, compact) if query.casefold()))


def competitor_bing_query(title: str, keyword: str) -> str:
    """Return a natural Bing query that retains the article's reader intent.

    Bing can over-weight the token ``LED`` and return electronics definitions
    for a product-shaped keyword. Inspiration titles need a human phrasing
    such as ``outdoor stair lighting ideas`` rather than a bag of product
    terms, while other intents retain the compact query used for Google.
    """
    title_intent = " ".join(title.casefold().split())
    inspiration = any(marker in title_intent for marker in (" idea", "inspiration", "inspiring", "tips", "examples", "designs", "styles"))
    if inspiration and "outdoor" in title_intent and "stair" in title_intent:
        return "outdoor stair lighting ideas guide"
    queries = competitor_search_queries(title, keyword)
    return queries[-1] if queries else " ".join(keyword.split()) or " ".join(title.split())


def competitor_candidate_exclusion_reason(item: Mapping[str, Any], own_domain: str) -> str | None:
    """Reject obvious non-editorial candidates before any page crawler runs."""
    domain = str(item.get("domain") or "").casefold()
    url = str(item.get("url") or "").casefold()
    title = str(item.get("title") or "").casefold()
    if own_domain and (domain == own_domain or domain.endswith("." + own_domain)):
        return "Current website page is excluded from competitor research."
    if any(domain == blocked or domain.endswith("." + blocked) for blocked in COMPETITOR_NON_ARTICLE_DOMAINS):
        return "Social, community, visual, or video platform result is excluded from competitor crawling and learning."
    if any(domain == marketplace or domain.endswith("." + marketplace) for marketplace in COMPETITOR_MARKETPLACE_DOMAINS):
        return "Marketplace listing is excluded before crawling because it is not an editorial article."
    if any(marker in url for marker in COMPETITOR_PRODUCT_PATH_MARKERS):
        return "Product, category, collection, or store page is excluded before crawling."
    if any(marker in title for marker in ("buy ", "shop ", "best sellers", "for sale", "product catalog")):
        return "Product-shopping result is excluded before crawling."
    return None


class KeywordDiscoveryServer(ThreadingHTTPServer):
    database_path: str | Path
    suggest_client: Any
    keyword_reviewer: Any | None
    title_generator: Any
    serp_title_client: Any
    ai_settings_path: Path
    content_generator: Any | None
    competitor_content_client: Any
    competitor_search_client: Any | None
    gsc_oauth_states: dict[str, int]
    gsc_browser_client: GscBrowserCaptureClient
    periodic_learning_stop: threading.Event
    periodic_learning_wake: threading.Event
    collection_service: CollectionService
    recovered_collection_runs: list[tuple[int, int]]
    recovered_content_agent_jobs: list[tuple[int, int]]
    recovered_gsc_feedback_jobs: list[tuple[int, int]]
    task_queue: DurableTaskQueue
    worker_token: str
    periodic_learning_thread: threading.Thread | None
    project_repository: ProjectRepository
    keyword_repository: KeywordRepository
    collection_repository: CollectionRepository
    runtime_database: RuntimeDatabaseController
    runtime_postgres_factory: PostgresRuntimeConnectionFactory | None
    credential_vault: CredentialVault

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.task_queue.start()
        if self.periodic_learning_thread is None:
            self.periodic_learning_thread = threading.Thread(
                target=_periodic_competitor_learning_loop,
                args=(self,),
                daemon=True,
                name="periodic-competitor-learning",
            )
            self.periodic_learning_thread.start()
        recovered = list(getattr(self, "recovered_collection_runs", []))
        self.recovered_collection_runs = []
        for project_id, run_id in recovered:
            self.enqueue_background_task("competitor_catalog_collection", project_id, run_id)
        for project_id, job_id in list(getattr(self, "recovered_content_agent_jobs", [])):
            self.enqueue_background_task("agent_job", project_id, job_id)
        self.recovered_content_agent_jobs = []
        for project_id, job_id in list(getattr(self, "recovered_gsc_feedback_jobs", [])):
            self.enqueue_background_task("agent_job", project_id, job_id)
        self.recovered_gsc_feedback_jobs = []
        super().serve_forever(poll_interval=poll_interval)

    def enqueue_background_task(self, task_type: str, project_id: int, resource_id: int) -> dict[str, Any]:
        return self.task_queue.enqueue(
            project_id=project_id,
            task_type=task_type,
            resource_id=resource_id,
            payload={"project_id": project_id},
        )

    def shutdown(self) -> None:
        if hasattr(self, "periodic_learning_stop"):
            self.periodic_learning_stop.set()
        if hasattr(self, "periodic_learning_wake"):
            self.periodic_learning_wake.set()
        if self.periodic_learning_thread is not None and self.periodic_learning_thread is not threading.current_thread():
            self.periodic_learning_thread.join(timeout=2)
        if hasattr(self, "task_queue"):
            self.task_queue.stop()
        super().shutdown()


class KeywordDiscoveryRequestHandler(SimpleHTTPRequestHandler):
    server: KeywordDiscoveryServer

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        collection_run_items = re.fullmatch(r"/api/competitor-url-catalog/collection-runs/(\d+)/items", path)
        agent_job = re.fullmatch(r"/api/agent-jobs/(\d+)", path)
        learning_memory = re.fullmatch(r"/api/content-learning-memories/(\d+)", path)
        competitor_learning = re.fullmatch(r"/api/projects/(\d+)/competitor-learning(?:/(runs))?", path)
        wordpress_project = re.fullmatch(r"/api/projects/(\d+)/wordpress", path)
        gsc_project = re.fullmatch(r"/api/projects/(\d+)/gsc(?:/(anchors|export\.csv|content-performance|content-effectiveness))?", path)
        gsc_authorize = re.fullmatch(r"/api/projects/(\d+)/gsc/authorize", path)
        gsc_browser_open = re.fullmatch(r"/api/projects/(\d+)/gsc/browser/open", path)
        knowledge_project = re.fullmatch(r"/api/projects/(\d+)/knowledge", path)
        crawl_run = re.fullmatch(r"/api/projects/(\d+)/knowledge/crawl/(\d+)", path)
        if competitor_learning:
            self._get_competitor_learning(int(competitor_learning.group(1)), include_runs=competitor_learning.group(2) == "runs")
        elif learning_memory:
            self._get_content_learning_memory(int(learning_memory.group(1)))
        elif agent_job:
            self._get_agent_job(int(agent_job.group(1)))
        elif gsc_browser_open:
            self._open_gsc_browser(int(gsc_browser_open.group(1)))
        elif path == "/api/gsc/oauth/callback":
            self._gsc_oauth_callback(parse_qs(urlsplit(self.path).query))
        elif gsc_authorize:
            self._start_gsc_oauth(int(gsc_authorize.group(1)))
        elif gsc_project:
            if gsc_project.group(2) == "anchors": self._list_gsc_anchor_candidates(int(gsc_project.group(1)))
            elif gsc_project.group(2) == "export.csv": self._export_gsc_anchor_candidates(int(gsc_project.group(1)))
            elif gsc_project.group(2) == "content-performance": self._list_content_gsc_performance(int(gsc_project.group(1)))
            elif gsc_project.group(2) == "content-effectiveness": self._list_content_effectiveness(int(gsc_project.group(1)))
            else: self._get_gsc_project(int(gsc_project.group(1)))
        elif wordpress_project:
            self._get_wordpress_config(int(wordpress_project.group(1)))
        elif crawl_run:
            self._get_knowledge_crawl_run(int(crawl_run.group(1)), int(crawl_run.group(2)))
        elif knowledge_project:
            self._list_project_knowledge(int(knowledge_project.group(1)))
        elif path == "/api/projects/summary":
            self._list_project_summaries()
        elif path == "/api/runtime-database/status":
            self._runtime_database_status()
        elif path == "/api/system-tasks":
            self._list_system_tasks()
        elif path == "/api/agent-jobs":
            self._list_agent_jobs()
        elif path == "/api/content-learning-memories":
            self._list_content_learning_memories()
        elif path == "/api/keywords":
            self._list_keywords()
        elif path == "/api/projects":
            self._list_projects()
        elif path == "/api/settings/ai":
            self._get_ai_settings()
        elif path == "/api/settings/serper":
            self._get_serper_settings()
        elif path == "/api/settings/images":
            self._get_image_generation_settings()
        elif path == "/api/settings/gsc":
            self._get_gsc_settings()
        elif path == "/api/title-library":
            self._list_title_library()
        elif path == "/api/serp-title-samples":
            self._list_serp_title_samples()
        elif path == "/api/content-assets":
            self._list_content_assets()
        elif path == "/api/content-library":
            self._list_content_library()
        elif path == "/api/content-memory":
            self._list_content_memory()
        elif path == "/api/competitor-content-learning/runs":
            self._list_competitor_content_learning_runs()
        elif path == "/api/competitor-url-archive":
            self._list_competitor_url_archive()
        elif collection_run_items:
            self._list_competitor_catalog_collection_run_items(int(collection_run_items.group(1)))
        elif path == "/api/competitor-url-catalog/collection-runs":
            self._list_competitor_catalog_collection_runs()
        elif path == "/api/collection-plans":
            self._list_collection_plans()
        elif path == "/api/competitor-url-catalog":
            self._list_competitor_url_catalog()
        elif path == "/api/authority-sources":
            self._list_authority_sources()
        elif self._content_asset_path(path) is not None:
            self._get_content_asset(self._content_asset_path(path) or 0)
        elif self._keyword_title_candidates_path(path) is not None:
            self._list_title_candidates(self._keyword_title_candidates_path(path) or 0)
        elif path in {"", "/", "/agent-platform", "/projects", "/system-tasks", "/integrations", "/research", "/keywords", "/titles", "/title-library", "/content", "/content-history", "/content-library", "/content-memory", "/collected-content-library", "/competitor-learning", "/learning-memories", "/knowledge", "/website-crawl", "/gsc", "/content-publish", "/authority-sources", "/scoring", "/settings"} or re.fullmatch(r"/content-library/\d+", path) or re.fullmatch(r"/(agent-platform/site|projects)(/\d+)?(?:/.*)?", path):
            self._serve_index()
        else:
            super().do_GET()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        internal_queue_execute = re.fullmatch(r"/api/internal/task-queue/(\d+)/execute", path)
        agent_job_action = re.fullmatch(r"/api/agent-jobs/(\d+)/(retry|cancel|pause|resume|execute)", path)
        agent_approval = re.fullmatch(r"/api/agent-approvals/(\d+)", path)
        learning_memory_action = re.fullmatch(r"/api/content-learning-memories/(\d+)/(disable|enable|pin|unpin|feedback)", path)
        competitor_learning = re.fullmatch(r"/api/projects/(\d+)/competitor-learning(?:/(run|runs/(\d+)/execute))?", path)
        candidate_id = self._title_candidate_action_path(path, "select")
        content_action = self._content_asset_action_path(path)
        content_prompt_preview = re.fullmatch(r"/api/content-assets/(\d+)/prompt-preview", path)
        wordpress_project = re.fullmatch(r"/api/projects/(\d+)/wordpress(?:/(test))?", path)
        gsc_project = re.fullmatch(r"/api/projects/(\d+)/gsc/(property|sync|learn-content)", path)
        gsc_browser_capture = re.fullmatch(r"/api/projects/(\d+)/gsc/browser/(capture|capture-ranked-pages)", path)
        knowledge_project = re.fullmatch(r"/api/projects/(\d+)/knowledge(?:/(crawl))?", path)
        image_generate = re.fullmatch(r"/api/content-images/(\d+)/generate", path)
        catalog_collection_execute = re.fullmatch(r"/api/competitor-url-catalog/collection-runs/(\d+)/execute", path)
        collection_plan_discover = re.fullmatch(r"/api/collection-plans/(\d+)/discover", path)
        collected_content_learning_execute = re.fullmatch(r"/api/competitor-content-learning/runs/(\d+)/execute", path)
        runtime_database_action = re.fullmatch(r"/api/runtime-database/(shadow/enable|cutover/check|cutover|rollback)", path)
        if path not in {"/api/projects", "/api/keyword-imports", "/api/suggest-expansions", "/api/keyword-opportunity-scores", "/api/expanded-keywords", "/api/ai-keyword-reviews", "/api/serp-title-research", "/api/browser-serp-title-research", "/api/title-generation-jobs", "/api/multi-provider-title-generation-jobs", "/api/title-candidates", "/api/content-assets", "/api/authority-sources", "/api/authority-sources/research", "/api/settings/ai", "/api/settings/ai/test", "/api/settings/serper", "/api/settings/serper/test", "/api/settings/images", "/api/settings/images/test", "/api/settings/gsc", "/api/agent-jobs", "/api/content-learning-memories", "/api/collection-plans", "/api/competitor-url-catalog/collect", "/api/competitor-content-learning/learn"} and internal_queue_execute is None and candidate_id is None and content_action is None and content_prompt_preview is None and wordpress_project is None and gsc_project is None and gsc_browser_capture is None and knowledge_project is None and image_generate is None and agent_job_action is None and agent_approval is None and learning_memory_action is None and competitor_learning is None and catalog_collection_execute is None and collection_plan_discover is None and collected_content_learning_execute is None and runtime_database_action is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        payload = self._read_json()
        if payload is None:
            return
        if internal_queue_execute:
            self._execute_durable_queue_job(int(internal_queue_execute.group(1)))
        elif runtime_database_action:
            self._runtime_database_action(runtime_database_action.group(1), payload)
        elif collection_plan_discover:
            self._discover_collection_plan(int(collection_plan_discover.group(1)), payload)
        elif path == "/api/collection-plans":
            self._save_collection_plan(payload)
        elif path == "/api/competitor-url-catalog/collect":
            self._queue_competitor_catalog_collection(payload)
        elif catalog_collection_execute:
            self._execute_competitor_catalog_collection(int(catalog_collection_execute.group(1)), payload)
        elif path == "/api/competitor-content-learning/learn":
            self._queue_collected_competitor_content_learning(payload)
        elif collected_content_learning_execute:
            self._execute_collected_competitor_content_learning(int(collected_content_learning_execute.group(1)), payload)
        elif competitor_learning:
            project_id, action, run_id = int(competitor_learning.group(1)), competitor_learning.group(2), competitor_learning.group(3)
            if action == "run":
                self._queue_competitor_learning_run(project_id, payload, trigger_type="manual")
            elif run_id is not None:
                self._execute_competitor_learning_run(project_id, int(run_id))
            else:
                self._save_competitor_learning_schedule(project_id, payload)
        elif learning_memory_action:
            memory_id, action = int(learning_memory_action.group(1)), learning_memory_action.group(2)
            if action in {"disable", "enable"}:
                self._set_content_learning_memory_status(memory_id, action, payload)
            elif action in {"pin", "unpin"}:
                self._set_content_learning_memory_pin(memory_id, action, payload)
            else:
                self._add_content_learning_memory_feedback(memory_id, payload)
        elif path == "/api/content-learning-memories":
            self._create_content_learning_memory(payload)
        elif agent_job_action:
            action = agent_job_action.group(2)
            if action == "retry": self._retry_agent_job(int(agent_job_action.group(1)), payload)
            elif action == "cancel": self._cancel_agent_job(int(agent_job_action.group(1)), payload)
            elif action == "pause": self._pause_agent_job(int(agent_job_action.group(1)), payload)
            elif action == "resume": self._resume_agent_job(int(agent_job_action.group(1)), payload)
            else: self._execute_agent_job(int(agent_job_action.group(1)), payload)
        elif agent_approval:
            self._decide_agent_approval(int(agent_approval.group(1)), payload)
        elif path == "/api/agent-jobs":
            self._create_agent_job(payload)
        elif wordpress_project:
            if wordpress_project.group(2): self._test_wordpress_config(int(wordpress_project.group(1)), payload)
            else: self._save_wordpress_config(int(wordpress_project.group(1)), payload)
        elif gsc_project:
            if gsc_project.group(2) == "property": self._save_gsc_property(int(gsc_project.group(1)), payload)
            elif gsc_project.group(2) == "learn-content": self._learn_from_published_gsc_content(int(gsc_project.group(1)), payload)
            else: self._sync_gsc_rankings(int(gsc_project.group(1)), payload)
        elif gsc_browser_capture:
            if gsc_browser_capture.group(2) == "capture-ranked-pages": self._capture_gsc_ranked_pages(int(gsc_browser_capture.group(1)))
            else: self._capture_gsc_browser_rows(int(gsc_browser_capture.group(1)))
        elif knowledge_project:
            if knowledge_project.group(2): self._crawl_project_knowledge(int(knowledge_project.group(1)), payload)
            else: self._create_project_knowledge(int(knowledge_project.group(1)), payload)
        elif image_generate:
            self._generate_section_image(int(image_generate.group(1)), payload)
        elif path == "/api/projects":
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
        elif path == "/api/authority-sources":
            self._create_authority_source(payload)
        elif path == "/api/authority-sources/research":
            self._research_authority_sources(payload)
        elif content_prompt_preview:
            self._preview_content_prompt(int(content_prompt_preview.group(1)), payload)
        elif content_action is not None:
            asset_id, action = content_action
            if action == "briefs": self._create_content_brief(asset_id, payload)
            elif action == "outlines": self._create_content_outline(asset_id, payload)
            elif action == "preview-competitors": self._preview_competitor_research_api(asset_id, payload)
            elif action == "preview-outline": self._preview_competitor_outline_api(asset_id, payload)
            elif action == "preview-content": self._preview_content_api(asset_id, payload)
            elif action == "restart": self._restart_content_asset(asset_id, payload)
            elif action == "research-competitors": self._research_competitors_api(asset_id, payload)
            elif action == "image-prompts": self._create_section_image_prompts(asset_id, payload)
            elif action == "generate-images": self._generate_all_section_images(asset_id, payload)
            elif action == "prepare-publish": self._prepare_wordpress_publish(asset_id, payload)
            elif action == "publish-wordpress": self._publish_wordpress(asset_id, payload)
            else: self._generate_content(asset_id, action, payload)
        elif path == "/api/settings/ai":
            self._save_ai_settings(payload)
        elif path == "/api/settings/ai/test":
            self._test_ai_settings(payload)
        elif path == "/api/settings/serper":
            self._save_serper_settings(payload)
        elif path == "/api/settings/serper/test":
            self._test_serper_settings(payload)
        elif path == "/api/settings/images":
            self._save_image_generation_settings(payload)
        elif path == "/api/settings/images/test":
            self._test_image_generation_settings(payload)
        elif path == "/api/settings/gsc":
            self._save_gsc_settings(payload)
        elif candidate_id is not None:
            self._select_title_candidate(candidate_id, payload)
        else:
            self._expand_suggestions(payload)

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        project_match = re.fullmatch(r"/api/projects/(\d+)", path)
        if project_match:
            self._delete_project(int(project_match.group(1)))
            return
        candidate_id = self._title_candidate_path(path)
        content_asset_id = self._content_asset_path(path)
        memory_id = self._content_memory_path(path)
        authority_match = re.fullmatch(r"/api/authority-sources/(\d+)", path)
        knowledge_match = re.fullmatch(r"/api/projects/(\d+)/knowledge/(\d+)", path)
        authority_id = int(authority_match.group(1)) if authority_match else None
        if path not in {"/api/keywords", "/api/content-assets", "/api/title-candidates"} and candidate_id is None and content_asset_id is None and memory_id is None and authority_id is None and knowledge_match is None:
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
        elif payload is not None and memory_id is not None:
            self._delete_content_memory(memory_id, payload)
        elif payload is not None and authority_id is not None:
            self._delete_authority_source(authority_id, payload)
        elif knowledge_match is not None:
            self._delete_project_knowledge(int(knowledge_match.group(1)), int(knowledge_match.group(2)))
        elif payload is not None:
            self._delete_keywords(payload)

    def do_PUT(self) -> None:
        path = urlsplit(self.path).path
        project_match = re.fullmatch(r"/api/projects/(\d+)", path)
        learning_memory = re.fullmatch(r"/api/content-learning-memories/(\d+)", path)
        if project_match is None and learning_memory is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        payload = self._read_json()
        if payload is not None and project_match is not None:
            self._update_project(int(project_match.group(1)), payload)
        elif payload is not None and learning_memory is not None:
            self._update_content_learning_memory(int(learning_memory.group(1)), payload)

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
            rows = [self._ensure_content_asset_tags(connection, row) for row in rows]
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
            rows = [self._ensure_content_asset_tags(connection, row) for row in rows]
        self._json(HTTPStatus.OK, [self._content_asset_payload(row) for row in rows])

    def _get_content_asset(self, asset_id: int) -> None:
        values = parse_qs(urlsplit(self.path).query).get("project_id", [])
        if len(values) != 1: self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id is required."}); return
        with self._database() as connection:
            payload = self._content_asset_detail(connection, int(values[0]), asset_id)
        self._json(HTTPStatus.OK, payload)

    def _preview_content_prompt(self, asset_id: int, payload: Mapping[str, Any]) -> None:
        """Expose the effective static prompt and scoped source roles before a billable run.

        This endpoint deliberately does not call a model and never returns raw
        company documents, GSC metrics, credentials, or competitor bodies. It
        gives the operator the exact server-side prompt instructions plus an
        auditable count of the project-local inputs that the next run may use.
        """
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        requested_action = self._optional_text(payload, "preview_action") or "generate"
        if requested_action not in {"generate", "generate-brief", "generate-outline", "generate-draft", "full_content_agent"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "preview_action is not supported."})
            return
        try:
            with self._database() as connection:
                asset = self._content_asset(connection, project_id, asset_id)
                manual_sources = self._content_sources(payload.get("sources", []))
                authority_sources = self._authority_sources_for_asset(connection, asset)
                gsc_sources = self._gsc_performance_sources_for_asset(connection, asset)
                competitor_sources, _analysis = self._research_sources(connection, asset["id"])
                learning_memories = self._select_content_learning_memories(connection, asset)
                outline_row = connection.execute("SELECT * FROM content_outlines WHERE id=?", (asset["current_outline_id"],)).fetchone() if asset["current_outline_id"] is not None else None
                outline_sections = self._content_outline_payload(connection, outline_row)["sections"] if outline_row is not None else []
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        source_groups = [
            ("网站产品与公司资料", "company_knowledge", [source for source in manual_sources if source.get("source_type") == "company_knowledge"], "只支持已提供的一方产品、品牌和公开业务事实。"),
            ("GSC 搜索信号", "gsc", [*gsc_sources, *[source for source in manual_sources if source.get("source_type") == "gsc_anchor"]], "只用于搜索措辞、内容缺口、页面重叠和内链机会；不会写入指标。"),
            ("竞品学习与调研", "competitor", competitor_sources, "只用于结构、术语、读者问题和选题缺口；不能复制或作为事实。"),
            ("权威资料", "authority", authority_sources, "用于技术、安全、认证、规格和法规等可验证事实。"),
            ("人工补充资料", "manual", [source for source in manual_sources if source.get("source_type") not in {"company_knowledge", "gsc_anchor"}], "仅按原始资料可支持的范围使用。"),
            ("项目学习记忆", "memory", learning_memories, "只提供已治理的写法、结构、选题或表现策略，不作为事实来源。"),
        ]
        source_summary = [
            {
                "key": key,
                "label": label,
                "count": len(items),
                "rule": rule,
                "examples": [str(item.get("title") or item.get("topic") or "项目资料")[:160] for item in items[:3] if isinstance(item, Mapping)],
            }
            for label, key, items, rule in source_groups
        ]
        prompt_sections = []
        for section in outline_sections:
            if not isinstance(section, Mapping):
                continue
            prepared = dict(section)
            prompt_sections.append({
                "heading": str(prepared.get("heading") or ""),
                "keyword_requirements": self._section_keyword_requirements(prepared, str(asset["keyword"] or "")),
                "depth_requirements": self._section_depth_requirements(prepared),
            })
        stages = ["industry_rules", "semantic", "title", "outline", "full_article", "qa"]
        if requested_action == "generate-brief": stages = ["industry_rules", "semantic"]
        elif requested_action == "generate-outline": stages = ["title", "outline"]
        elif requested_action == "generate-draft": stages = ["full_article"]
        self._json(HTTPStatus.OK, {
            "prompt_version": PROMPT_VERSION,
            "requested_action": requested_action,
            "article": {
                "title": str(asset["title_snapshot"] or ""),
                "primary_keyword": str(asset["keyword"] or ""),
                "locale": str(asset["locale"] or ""),
                "target_audience": self._optional_text(payload, "target_audience") or "US searchers evaluating this topic",
                "business_goal": self._optional_text(payload, "business_goal") or "informational",
            },
            "source_summary": source_summary,
            "full_article_requirements": {
                "sections": prompt_sections,
                "overall": self._article_depth_requirements(prompt_sections) if prompt_sections else None,
            },
            "system_prompt": SYSTEM_PROMPT,
            "stages": [{"stage": stage, "instruction": _stage_instruction(stage)} for stage in stages],
        })

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
                    cursor = connection.execute("INSERT INTO content_briefs(content_asset_id,target_audience,business_goal,target_length,sources_json,brief_json) VALUES(?,?,?,?,?,?)", (asset_id, audience, goal, target, json.dumps(sources, ensure_ascii=False), json.dumps({"source_policy": "unsupported facts are omitted or replaced with general, non-factual guidance"})))
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
                self._require_content_competitor_learning(connection, asset)
                with connection:
                    cursor = connection.execute("INSERT INTO content_outlines(content_asset_id,brief_id) VALUES(?,?)", (asset_id, brief_id))
                    for position, section in enumerate(sections, 1):
                        if not isinstance(section, Mapping) or not all(isinstance(section.get(key), str) and section[key].strip() for key in ("heading", "purpose")): raise ValueError("each outline section needs heading and purpose")
                        section_data = self._normalise_outline_section(section, position)
                        connection.execute("INSERT INTO content_outline_sections(outline_id,position,heading,purpose,word_budget,section_json) VALUES(?,?,?,?,?,?)", (cursor.lastrowid, position, section_data["heading"], section_data["purpose"], max(0, int(section_data.get("target_words") or 0)), json.dumps(section_data, ensure_ascii=False)))
                    connection.execute("UPDATE content_assets SET status='outlining',current_outline_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (cursor.lastrowid, asset_id))
                    row = connection.execute("SELECT * FROM content_outlines WHERE id=?", (cursor.lastrowid,)).fetchone()
                    result = self._content_outline_payload(connection, row)
        except (sqlite3.Error, ValueError) as error: self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.CREATED, result)

    def _research_competitors_api(self, asset_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection:
                asset = self._content_asset(connection, project_id, asset_id)
                # Competitor research is a learning task, not article writing.
                # Keep it independent from the GPT writer route.
                generator, provider, model = self._content_generator({**payload, "provider": "deepseek"})
                if generator is None:
                    raise ValueError("Selected content provider is not configured.")
                research = self._run_competitor_research(connection, asset, generator, provider, model)
        except CompetitorContentProtocolError as error:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)}); return
        except (sqlite3.Error, ValueError, ContentGenerationProtocolError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.CREATED, research)

    def _preview_competitor_research_api(self, asset_id: int, payload: Mapping[str, Any]) -> None:
        """Test top Google results without persisting content, memory, or AI output."""
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection:
                asset = self._content_asset(connection, project_id, asset_id)
                query = " ".join(str(asset["title_snapshot"] or "").split())
                if not query:
                    raise ValueError("a selected title is required for competitor preview")
                key = _serper_api_key(self.server.ai_settings_path)
                if key is None:
                    raise ValueError("Serper.dev · Google Search API is not configured.")
                client = SerperSearchClient(key)
                results: list[dict[str, Any]] = []
                seen: set[str] = set()
                for page in range(1, 6):
                    if len(results) >= 50:
                        break
                    found = client.search(query=query, locale=asset["locale"], max_results=min(10, 50 - len(results)), page=page)
                    added = 0
                    for item in found:
                        normalized = normalize_competitor_url(str(item.get("url") or ""))
                        if not normalized or normalized in seen:
                            continue
                        seen.add(normalized); added += 1
                        value = dict(item); value["rank"] = len(results) + 1
                        results.append(value)
                        if len(results) >= 50:
                            break
                    if not added:
                        break
                own_domain = self._project_domain(connection, project_id)
            eligible = [item for item in results if not competitor_candidate_exclusion_reason(item, own_domain)]
            # The preview is intentionally a fast, no-write diagnostic.  It
            # lists the complete first 50 Google results, then checks the top
            # 20 editorial candidates for static readable text.  Crawling all
            # 50 with JS/Crawl4AI fallbacks could hold the button for several
            # minutes and looked like an unresponsive UI.
            preview_candidates = eligible[:20]
            preview_urls = [str(item["url"]) for item in preview_candidates]
            preview_extract = getattr(self.server.competitor_content_client, "preview_extract_many", None)
            if callable(preview_extract):
                batch = preview_extract(preview_urls, max_workers=5, respect_robots=True)
            else:
                # Keep injected clients and older integrations compatible.
                batch = self.server.competitor_content_client.extract_many(preview_urls, max_workers=5, respect_robots=True)
            items: list[dict[str, Any]] = []
            for item in results:
                reason = competitor_candidate_exclusion_reason(item, own_domain)
                value = {"rank": item["rank"], "title": item["title"], "url": item["url"], "domain": item["domain"]}
                if reason:
                    value.update({"status": "skipped", "reason": reason, "content_chars": 0})
                elif str(item["url"]) not in batch:
                    value.update({"status": "skipped", "reason": "未纳入本次快速测试（只检测前 20 条可学习候选）；正式采集会按完整策略继续筛选。", "content_chars": 0})
                else:
                    extracted = batch.get(str(item["url"]))
                    if isinstance(extracted, Mapping):
                        value.update({"status": "available", "reason": "正文可读取；正式采集后才会入库和参与学习。", "content_chars": len(str(extracted.get("content") or ""))})
                    else:
                        value.update({"status": "failed", "reason": str(extracted or "正文提取失败。"), "content_chars": 0})
                items.append(value)
            # Preview bodies are intentionally process-local and short-lived:
            # they are never written to SQLite/PostgreSQL or a learning table.
            # Reusing them for the immediately following outline test prevents
            # the UI from downloading the same five pages a second time.
            cache = getattr(self.server, "competitor_preview_cache", {})
            cache[(project_id, asset_id)] = {
                "created_at": time.monotonic(),
                "pages": {
                    url: {
                        "title": str(page.get("title") or ""),
                        "content": str(page.get("content") or ""),
                        "domain": str(page.get("domain") or ""),
                    }
                    for url, page in batch.items()
                    if isinstance(page, Mapping) and str(page.get("content") or "").strip()
                },
            }
            self.server.competitor_preview_cache = cache
        except (sqlite3.Error, ValueError, SerperSearchProtocolError, CompetitorContentProtocolError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        available = sum(1 for item in items if item["status"] == "available")
        self._json(HTTPStatus.OK, {"query": query, "discovered_count": len(items), "available_count": available, "recommended_learning_count": min(5, available), "items": items})

    def _preview_competitor_outline_api(self, asset_id: int, payload: Mapping[str, Any]) -> None:
        """Generate a disposable outline from user-tested sources only."""
        project_id = self._integer(payload, "project_id")
        raw_sources = payload.get("sources")
        if project_id is None or not isinstance(raw_sources, list):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id and tested sources are required."}); return
        sources = [item for item in raw_sources[:5] if isinstance(item, Mapping) and isinstance(item.get("url"), str) and str(item.get("url")).startswith(("http://", "https://"))]
        if not sources:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "请先完成测试采集，并至少选择一篇可读取文章。"}); return
        try:
            with self._database() as connection:
                asset = self._content_asset(connection, project_id, asset_id)
            urls = [str(item["url"]) for item in sources]
            cached_preview = getattr(self.server, "competitor_preview_cache", {}).get((project_id, asset_id), {})
            cached_pages = cached_preview.get("pages", {}) if isinstance(cached_preview, Mapping) and time.monotonic() - float(cached_preview.get("created_at") or 0) <= 600 else {}
            missing_urls = [url for url in urls if not isinstance(cached_pages.get(url), Mapping)]
            preview_extract = getattr(self.server.competitor_content_client, "preview_extract_many", None)
            if missing_urls:
                batch = preview_extract(missing_urls, max_workers=5, respect_robots=True) if callable(preview_extract) else self.server.competitor_content_client.extract_many(missing_urls, max_workers=5, respect_robots=True)
            else:
                batch = {}
            pages = []
            for item in sources:
                url = str(item["url"])
                page = cached_pages.get(url) if isinstance(cached_pages.get(url), Mapping) else batch.get(url)
                if not isinstance(page, Mapping):
                    continue
                pages.append({"url": item["url"], "search_query": asset["title_snapshot"], "search_title": item.get("title", ""), "page_title": page.get("title", ""), "domain": page.get("domain", item.get("domain", "")), "content_excerpt": str(page.get("content", ""))[:5000]})
            if not pages:
                raise CompetitorContentProtocolError("测试来源无法再次读取正文，无法生成测试大纲。")
            generator, _provider, _model = self._content_generator({"provider": "deepseek"})
            if generator is None:
                raise ValueError("Selected content provider is not configured.")
            data = {"target_keyword": asset["keyword"], "selected_title": asset["title_snapshot"], "locale": asset["locale"], "pages": pages}
            raw = generator.run_stage(stage="competitor_analysis", data=data) if callable(getattr(generator, "run_stage", None)) else generator.generate(stage="competitor_analysis", **data)
            analysis = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(analysis, Mapping) or not isinstance(analysis.get("dynamic_outline"), list):
                raise ContentGenerationProtocolError("AI returned no usable test outline.")
        except (sqlite3.Error, ValueError, CompetitorContentProtocolError, ContentGenerationProtocolError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.OK, {"source_count": len(pages), "analysis": analysis, "persisted": False})

    def _preview_content_api(self, asset_id: int, payload: Mapping[str, Any]) -> None:
        """Generate one disposable H2 sample from already learned competitor sources."""
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection:
                asset = self._content_asset(connection, project_id, asset_id)
                sources, analysis = self._research_sources(connection, asset_id)
                if not sources or not isinstance(analysis, Mapping):
                    raise CompetitorContentProtocolError("请先完成同行采集与学习，再测试内容。")
                raw_outline = analysis.get("dynamic_outline")
                if not isinstance(raw_outline, list):
                    raise ContentGenerationProtocolError("同行学习没有返回可用于测试的文章大纲。")
                first_section = next((item for item in raw_outline if isinstance(item, Mapping) and str(item.get("heading") or "").strip()), None)
                if first_section is None:
                    raise ContentGenerationProtocolError("同行学习没有返回可用于测试的文章章节。")
                generator, provider, model = self._content_generator(payload)
                if generator is None:
                    raise ValueError("Selected content provider is not configured.")
                section = dict(first_section)
                section["id"] = "test-section-1"
                section["position"] = 1
                section["source_ids"] = [str(source["source_id"]) for source in sources[:5]]
                section["internal_link_plan"] = {"use": False}
                compact_sources = [{**source, "content": str(source.get("content") or "")[:6_000]} for source in sources[:5]]
                chapter_plan = {
                    "chapter_goal": str(section.get("purpose") or section.get("reader_question") or "Give the reader a practical, evidence-bounded answer."),
                    "subtopics": [{"heading": str(section.get("heading")), "reader_question": str(section.get("reader_question") or "What does the reader need to know?"), "key_points": section.get("key_points") or []}],
                    "internal_link_plan": {"use": False},
                    "product_recommendation": {"use": False},
                }
                data = {
                    "topic": asset["title_snapshot"],
                    "title": asset["title_snapshot"],
                    "audience": self._optional_text(payload, "target_audience") or "US searchers evaluating this topic",
                    "intent": {},
                    "project_context": {},
                    "writing_policy": {},
                    "angle": "",
                    "section": section,
                    "chapter_plan": chapter_plan,
                    "competitor_learning": analysis,
                    "learning_memories": self._select_content_learning_memories(connection, asset),
                    "sources": compact_sources,
                    "voice": self._optional_text(payload, "voice") or "clear, helpful American English",
                    "language": asset["locale"],
                    "reader_markdown_policy": "Output portable Markdown only. This is an unsaved H2 preview. Do not output internal source IDs, research labels, citations, or Markdown links.",
                }
                raw = generator.run_stage(stage="section", data=data) if callable(getattr(generator, "run_stage", None)) else generator.generate(stage="section", **data)
                result = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(result, Mapping) or not isinstance(result.get("markdown"), str) or not result["markdown"].strip():
                    raise ContentGenerationProtocolError("AI returned no usable test content.")
        except (sqlite3.Error, ValueError, CompetitorContentProtocolError, ContentGenerationProtocolError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {
            "heading": str(section.get("heading") or "测试章节"),
            "markdown": self._sanitize_reader_markdown(str(result["markdown"])),
            "source_count": len(compact_sources),
            "provider": provider,
            "model": model,
            "persisted": False,
        })

    def _restart_content_asset(self, asset_id: int, payload: Mapping[str, Any]) -> None:
        """Clear only the current title's production pointers before a fresh Agent run.

        Old versions, source snapshots and job/step audit rows stay in the database;
        the reset merely removes them from the active production path.
        """
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        if payload.get("confirm_reset") is not True:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "confirm_reset=true is required before restarting content production."})
            return
        try:
            with self._database() as connection:
                self._content_asset(connection, project_id, asset_id)
                with connection:
                    connection.execute(
                        "UPDATE agent_steps SET status='cancelled',completed_at=CURRENT_TIMESTAMP,error_summary=COALESCE(error_summary,'Cancelled because this title was restarted.') WHERE job_id IN (SELECT id FROM agent_jobs WHERE project_id=? AND content_asset_id=? AND status IN ('queued','planning','running','retrying','waiting_approval','waiting_input')) AND status IN ('queued','running')",
                        (project_id, asset_id),
                    )
                    connection.execute(
                        "UPDATE agent_jobs SET status='cancelled',current_node='cancelled',error_summary=COALESCE(error_summary,'Cancelled because this title was restarted.'),completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND content_asset_id=? AND status IN ('queued','planning','running','retrying','waiting_approval','waiting_input')",
                        (project_id, asset_id),
                    )
                    connection.execute(
                        "UPDATE content_generation_jobs SET status='failed',failed_stage='restarted',error_summary=COALESCE(error_summary,'Superseded by a fresh content Agent run.'),completed_at=CURRENT_TIMESTAMP WHERE project_id=? AND content_asset_id=? AND status='running'",
                        (project_id, asset_id),
                    )
                    connection.execute("UPDATE content_briefs SET status='superseded' WHERE content_asset_id=? AND status IN ('current','pending_approval')", (asset_id,))
                    connection.execute("UPDATE content_outlines SET status='superseded' WHERE content_asset_id=? AND status IN ('current','pending_approval','approved')", (asset_id,))
                    connection.execute("UPDATE content_drafts SET status='superseded' WHERE project_id=? AND content_asset_id=? AND status IN ('draft','current','approved')", (project_id, asset_id))
                    connection.execute("UPDATE competitor_research_runs SET status=CASE WHEN status='running' THEN 'failed' ELSE 'insufficient' END,error_summary=COALESCE(error_summary,'Superseded by a fresh content Agent run.'),completed_at=COALESCE(completed_at,CURRENT_TIMESTAMP) WHERE project_id=? AND content_asset_id=? AND status IN ('running','completed','insufficient')", (project_id, asset_id))
                    connection.execute("UPDATE content_assets SET status='planned',current_brief_id=NULL,current_outline_id=NULL,current_draft_id=NULL,current_generation_run_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?", (asset_id, project_id))
                result = self._content_asset_detail(connection, project_id, asset_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, result)

    def _run_competitor_research(self, connection: sqlite3.Connection, asset: sqlite3.Row, generator: Any, provider: str, model: str | None) -> dict[str, Any]:
        """Capture one to five accessible competitors and persist website/project memory."""
        google_query = " ".join(str(asset["title_snapshot"] or "").split())
        if not google_query:
            raise ValueError("a selected title is required for competitor research")
        # Google research intentionally follows the exact approved title.
        # If the title produces a product-heavy SERP, the UI offers a title
        # recovery flow instead of silently broadening or changing the query.
        queries = [google_query]
        with connection:
            cursor = connection.execute(
                "INSERT INTO competitor_research_runs(project_id,content_asset_id,query,locale,provider,model) VALUES(?,?,?,?,?,?)",
                (asset["project_id"], asset["id"], f"Serper.dev · Google API: {' → '.join(queries)}", asset["locale"], provider, model),
            )
            run_id = int(cursor.lastrowid)
            connection.execute("UPDATE competitor_research_runs SET query=? WHERE id=?", (f"Serper.dev · Google API: {' → '.join(queries)}", run_id))
        try:
            # The user-approved title is the only Google query.  A shopping-
            # heavy SERP is reported to the user rather than being silently
            # broadened with keyword or intent variants.
            results: list[dict[str, Any]] = []
            seen_urls: set[str] = set()
            search_errors: list[str] = []
            search_client = getattr(self.server, "competitor_search_client", None)
            if search_client is None:
                serper_key = _serper_api_key(self.server.ai_settings_path)
                if serper_key is None:
                    raise CompetitorContentProtocolError("Serper.dev · Google Search API is not configured. Save the Serper API key in AI & integrations before competitor research.")
                search_client = SerperSearchClient(serper_key)
            # One approved title equals one Google query.  Search intent is
            # therefore auditable and never silently broadened by the system.
            for query in queries:
                # Serper returns a page of organic results.  Keep the query
                # identical and continue past a social-heavy first page.
                # Only the best enterprise editorial candidates are fetched.
                for page_number in range(1, 6):
                    if len(results) >= 50:
                        break
                    try:
                        try:
                            found = search_client.search(
                                query=query,
                                locale=asset["locale"],
                                max_results=min(10, 50 - len(results)),
                                page=page_number,
                            )
                        except TypeError:
                            # Existing injected test/legacy adapters can still
                            # serve the first page; production Serper supports
                            # the explicit page parameter above.
                            if page_number != 1:
                                break
                            found = search_client.search(
                                query=query,
                                locale=asset["locale"],
                                max_results=min(10, 50 - len(results)),
                            )
                    except SerperSearchProtocolError as error:
                        search_errors.append(f"{query} (page {page_number}): {error}")
                        break
                    if not found:
                        break
                    added_on_page = 0
                    for item in found:
                        url = str(item.get("url") or "")
                        normalized = normalize_competitor_url(url)
                        if not normalized or normalized in seen_urls:
                            continue
                        seen_urls.add(normalized)
                        result = dict(item)
                        result["rank"] = len(results) + 1
                        result["search_query"] = query
                        results.append(result)
                        added_on_page += 1
                        if len(results) >= 50:
                            break
                    if added_on_page == 0:
                        break
            if not results:
                message = "Serper.dev · Google Search API returned no readable organic results for the selected title. " + " | ".join(search_errors)
                with connection:
                    connection.execute(
                        "UPDATE competitor_research_runs SET status='insufficient',error_summary=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                        (message, run_id),
                    )
                raise CompetitorContentProtocolError(message)
            selected: list[dict[str, Any]] = []
            own_domain = self._project_domain(connection, asset["project_id"])
            # The catalog is the durable local record of every organic URL
            # discovered for this project.  It intentionally includes URLs
            # later excluded from crawling, so the user can inspect the full
            # search landscape without retaining prohibited page bodies.
            for item in results:
                reason = competitor_candidate_exclusion_reason(item, own_domain)
                self._upsert_competitor_url_catalog(
                    connection,
                    asset["project_id"],
                    item,
                    collection_status="excluded" if reason else "queued",
                    reason=reason or "",
                )
            # Serper returns the first 20 Google organic results for the
            # selected title.  Every eligible result is fetched and stored;
            # the final AI outline receives a bounded evidence pack so the
            # model has enough room to analyse the articles rather than
            # truncating all of their bodies.
            def editorial_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
                candidates: list[dict[str, Any]] = []
                for item in items:
                    reason = competitor_candidate_exclusion_reason(item, own_domain)
                    if reason:
                        with connection:
                            connection.execute(
                                "INSERT INTO competitor_research_items(research_run_id,rank,search_title,url,domain,status,error_summary) VALUES(?,?,?,?,?, 'skipped',?)",
                                (run_id, item["rank"], item["title"], item["url"], item["domain"], reason),
                            )
                        continue
                    candidates.append(item)
                return candidates

            # Save and collect every eligible editorial URL from this search
            # run.  The AI is still given a deliberate five-source evidence
            # pack later, while the complete permitted corpus remains in the
            # project-local memory for long-term learning.
            # Write the discovery batch first, then read the queued rows back
            # from the project-local catalog.  This makes the catalog the
            # single source of truth for later collection/retry work rather
            # than relying on transient search-response objects.
            enterprise_path_markers = ("/blog", "/learn", "/resource", "/guide", "/insight", "/news", "/knowledge")
            eligible_results = sorted(
                editorial_candidates(results),
                key=lambda item: (0 if any(marker in urlsplit(str(item.get("url") or "")).path.casefold() for marker in enterprise_path_markers) else 1, int(item.get("rank") or 0)),
            )[:15]
            # A research retry must reuse a previously collected article when
            # it appears in the same title's SERP.  Previously this method
            # fetched only catalog rows still marked ``queued``; a successful
            # prior collection was therefore omitted from the new learning
            # pack and could incorrectly make a retry look like zero sources.
            candidates: list[dict[str, Any]] = []
            extracted_pages: list[tuple[Mapping[str, Any], Mapping[str, Any], int]] = []
            reused_memory_ids: set[int] = set()
            for result in eligible_results:
                normalized = normalize_competitor_url(str(result.get("url") or ""))
                existing = connection.execute(
                    """SELECT catalog.collection_status,memory.id,memory.url,memory.domain,memory.page_title,memory.content
                       FROM competitor_url_catalog catalog
                       JOIN competitor_content_memory memory ON memory.id=catalog.memory_id
                       WHERE catalog.project_id=? AND catalog.normalized_url=?
                         AND catalog.collection_status='collected'""",
                    (asset["project_id"], normalized),
                ).fetchone() if normalized else None
                if existing is not None and str(existing["content"] or "").strip():
                    memory_id = int(existing["id"])
                    if memory_id not in reused_memory_ids:
                        reused_memory_ids.add(memory_id)
                        extracted_pages.append((
                            result,
                            {
                                "title": str(existing["page_title"] or result["title"]),
                                "content": str(existing["content"]),
                                "domain": str(existing["domain"] or result["domain"]),
                            },
                            memory_id,
                        ))
                    continue
                candidates.append(result)
            batch = self.server.competitor_content_client.extract_many([str(item["url"]) for item in candidates], max_workers=5, respect_robots=True) if callable(getattr(self.server.competitor_content_client, "extract_many", None)) else {}
            for result in candidates:
                try:
                    fetched = batch.get(str(result["url"])) if batch else None
                    if isinstance(fetched, Exception):
                        raise fetched
                    page = fetched if isinstance(fetched, Mapping) else self._extract_competitor_content(url=str(result["url"]))
                    memory_id = self._upsert_competitor_memory(connection, asset["project_id"], result, page)
                    self._upsert_competitor_url_catalog(connection, asset["project_id"], result, collection_status="collected", memory_id=memory_id)
                    extracted_pages.append((result, page, memory_id))
                except Exception as error:
                    self._archive_robots_blocked_url(connection, asset["project_id"], result, error)
                    self._upsert_competitor_url_catalog(connection, asset["project_id"], result, collection_status="robots_blocked" if "robot" in str(error).casefold() else "failed", reason=str(error))
                    with connection:
                        connection.execute(
                            "INSERT INTO competitor_research_items(research_run_id,rank,search_title,url,domain,status,error_summary) VALUES(?,?,?,?,?, 'failed',?)",
                            (run_id, result["rank"], result["title"], result["url"], result["domain"], str(error)),
                        )
            # This content-research workflow deliberately uses the selected
            # title's Serper/Google results only. Bing remains available for
            # other tools, but is not mixed into this article's evidence pack.
            bing_search = None
            if callable(bing_search):
                bing_query = competitor_bing_query(str(asset["title_snapshot"]), str(asset["keyword"] or ""))
                try:
                    bing_found = bing_search(query=bing_query, locale=asset["locale"], max_results=20)
                except Exception as error:
                    search_errors.append(f"Bing fallback {bing_query}: {error}")
                    bing_found = []
                bing_results: list[dict[str, Any]] = []
                for item in bing_found:
                    url = str(item.get("url") or "")
                    normalized = url.split("#", 1)[0].rstrip("/").casefold()
                    if not normalized or normalized in seen_urls:
                        continue
                    seen_urls.add(normalized)
                    result = dict(item)
                    result["rank"] = len(results) + len(bing_results) + 1
                    result["search_query"] = f"Bing fallback: {bing_query}"
                    bing_results.append(result)
                if bing_results:
                    results.extend(bing_results)
                    with connection:
                        connection.execute("UPDATE competitor_research_runs SET query=? WHERE id=?", (f"Serper.dev · Google API: {' → '.join(queries)} | Bing fallback: {bing_query}", run_id))
                    bing_candidates = editorial_candidates(bing_results)
                    bing_batch = self.server.competitor_content_client.extract_many([str(item["url"]) for item in bing_candidates], max_workers=5, respect_robots=True) if callable(getattr(self.server.competitor_content_client, "extract_many", None)) else {}
                    for result in bing_candidates:
                        try:
                            fetched = bing_batch.get(str(result["url"])) if bing_batch else None
                            if isinstance(fetched, Exception):
                                raise fetched
                            page = fetched if isinstance(fetched, Mapping) else self._extract_competitor_content(url=str(result["url"]))
                            memory_id = self._upsert_competitor_memory(connection, asset["project_id"], result, page)
                            self._upsert_competitor_url_catalog(connection, asset["project_id"], result, collection_status="collected", memory_id=memory_id)
                            extracted_pages.append((result, page, memory_id))
                        except Exception as error:
                            self._archive_robots_blocked_url(connection, asset["project_id"], result, error)
                            self._upsert_competitor_url_catalog(connection, asset["project_id"], result, collection_status="robots_blocked" if "robot" in str(error).casefold() else "failed", reason=str(error))
                            with connection:
                                connection.execute(
                                    "INSERT INTO competitor_research_items(research_run_id,rank,search_title,url,domain,status,error_summary) VALUES(?,?,?,?,?, 'failed',?)",
                                    (run_id, result["rank"], result["title"], result["url"], result["domain"], str(error)),
                                )
            # Screen a broader bounded pool so a result page with several
            # good articles is not reduced to one source merely because one
            # of the first five candidates is unavailable or off-topic.  The
            # final learning pack remains capped at five sources.
            screened_pages = extracted_pages[:10]
            relevance_data = {
                "target_keyword": asset["keyword"],
                "selected_title": asset["title_snapshot"],
                "locale": asset["locale"],
                "pages": [
                    {"url": result["url"], "search_query": result.get("search_query", ""), "search_title": result["title"], "page_title": page.get("title", ""), "domain": page.get("domain", result["domain"]), "content_excerpt": str(page.get("content", ""))[:3000]}
                    for result, page, _memory_id in screened_pages
                ],
            }
            if not relevance_data["pages"]:
                message = "Competitor research stopped: no accessible non-product article pages were available for relevance screening."
                with connection:
                    connection.execute(
                        "UPDATE competitor_research_runs SET status='insufficient',error_summary=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                        (message, run_id),
                    )
                raise CompetitorContentProtocolError(message)
            raw_relevance = generator.run_stage(stage="competitor_relevance", data=relevance_data) if callable(getattr(generator, "run_stage", None)) else generator.generate(stage="competitor_relevance", **relevance_data)
            relevance = json.loads(raw_relevance) if isinstance(raw_relevance, str) else raw_relevance
            if not isinstance(relevance, Mapping) or not isinstance(relevance.get("items"), list):
                raise ContentGenerationProtocolError("AI competitor relevance screening returned invalid JSON.")
            decisions = {
                str(item.get("url")): item for item in relevance["items"]
                if isinstance(item, Mapping) and item.get("decision") in {"accept", "reject"} and isinstance(item.get("url"), str)
            }
            product_like_rejections = 0
            for result, page, memory_id in screened_pages:
                decision = decisions.get(str(result["url"]))
                accepted = bool(decision and decision.get("decision") == "accept")
                reason = str(decision.get("reason") or "Did not match the selected title and search intent.") if decision else "No relevance decision returned for this page."
                # Comparison SERPs commonly contain useful editorial guides for
                # a neighbouring fixture (for example, deck or landscape lights
                # rather than stair lights).  These pages can still teach the
                # exact solar-versus-wired decision without being copied into
                # the article.  Keep the AI gate as the default, but recover a
                # narrowly-defined editorial comparison page when the model is
                # stricter than the selected title's actual decision intent.
                if not accepted and self._is_adjacent_comparison_decision(
                    selected_title=str(asset["title_snapshot"]),
                    result=result,
                    page=page,
                ):
                    accepted = True
                    reason = (
                        "Accepted as a substantive adjacent-fixture comparison: "
                        "it covers the same solar-versus-wired outdoor-lighting decision."
                    )
                if not accepted:
                    if any(marker in reason.casefold() for marker in ("product", "retail", "sale", "commerce", "listing", "deal", "shopper", "sku", "category")):
                        product_like_rejections += 1
                    with connection:
                        connection.execute(
                            "INSERT INTO competitor_research_items(research_run_id,rank,search_title,url,domain,status,error_summary) VALUES(?,?,?,?,?, 'skipped',?)",
                            (run_id, result["rank"], result["title"], result["url"], result["domain"], reason),
                        )
                    continue
                if len(selected) >= 5:
                    with connection:
                        connection.execute(
                            "INSERT INTO competitor_research_items(research_run_id,rank,search_title,url,domain,status,error_summary) VALUES(?,?,?,?,?, 'skipped',?)",
                            (run_id, result["rank"], result["title"], result["url"], result["domain"], "Five complementary competitor articles have already been selected for this learning run."),
                        )
                    continue
                with connection:
                    connection.execute(
                        "INSERT INTO competitor_research_items(research_run_id,memory_id,rank,search_title,url,domain,status,error_summary) VALUES(?,?,?,?,?,?, 'selected', ?)",
                        (run_id, memory_id, result["rank"], result["title"], result["url"], result["domain"], str(decision.get("reason") or "Relevant content page selected for structure and coverage learning.")),
                    )
                selected.append({"source_id": f"competitor-{memory_id}", "source_type": "competitor_page", "availability": "available", "url": result["url"], "title": page["title"], "publisher": page["domain"], "content": page["content"]})
            with connection:
                connection.execute("UPDATE competitor_research_runs SET discovered_count=?,usable_count=? WHERE id=?", (len(results), len(selected), run_id))
            # A single verified, relevant article is sufficient to produce an
            # evidence-bounded outline.  More accessible content naturally
            # enriches the research pack, up to the five pages crawled above;
            # zero sources is the only unsafe state because the model would
            # otherwise be forced to invent competitor evidence.
            if not selected:
                product_dominant = product_like_rejections >= 3 and product_like_rejections >= len(extracted_pages) / 2
                guidance = (
                    "The SERP is dominated by product-display and retail pages, so this title is not suitable for an SEO long-form article. "
                    "Choose a researchable informational title (for example, installation, IP ratings, selection, comparison, maintenance, or troubleshooting)."
                    if product_dominant else
                    "Choose a more natural, researchable informational title and retry."
                )
                with connection:
                    connection.execute("UPDATE competitor_research_runs SET status='insufficient',error_summary=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (f"Competitor research stopped: no usable, relevant competitor articles were available after crawling the top-ranked non-product Google results. {guidance}", run_id))
                raise CompetitorContentProtocolError(f"Competitor research stopped: no usable, relevant competitor articles were available after crawling the top-ranked non-product Google results. {guidance}")
            # Keep every accepted article in the website memory, but bound
            # this individual outline analysis to five sources so the model
            # receives a deliberate, stable evidence pack rather than dozens
            # of full pages in one request.
            analysis_data = {"target_keyword": asset["keyword"], "selected_title": asset["title_snapshot"], "locale": asset["locale"], "competitors_content": selected[:5]}
            raw = generator.run_stage(stage="competitor_analysis", data=analysis_data) if callable(getattr(generator, "run_stage", None)) else generator.generate(stage="competitor_analysis", **analysis_data)
            analysis = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(analysis, Mapping) or not isinstance(analysis.get("dynamic_outline"), list) or not analysis["dynamic_outline"]:
                raise ContentGenerationProtocolError("AI competitor analysis returned no usable dynamic outline.")
            value = dict(analysis)
            with connection:
                connection.execute("UPDATE competitor_research_runs SET status='completed',analysis_json=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(value, ensure_ascii=False), run_id))
                connection.execute("UPDATE content_assets SET status='briefing',updated_at=CURRENT_TIMESTAMP WHERE id=?", (asset["id"],))
            return self._competitor_research_payload(connection, run_id)
        except Exception as error:
            with connection:
                row = connection.execute("SELECT status FROM competitor_research_runs WHERE id=?", (run_id,)).fetchone()
                if row and row["status"] == "running":
                    connection.execute("UPDATE competitor_research_runs SET status='failed',error_summary=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (str(error), run_id))
            raise

    @staticmethod
    def _project_domain(connection: sqlite3.Connection, project_id: int) -> str:
        row = connection.execute("SELECT site_url FROM projects WHERE id=?", (project_id,)).fetchone()
        value = row[0] if row else None
        from urllib.parse import urlparse
        return urlparse(str(value)).hostname.removeprefix("www.") if isinstance(value, str) and urlparse(str(value)).hostname else ""

    def _extract_competitor_content(self, *, url: str) -> Mapping[str, Any]:
        """Fetch a public article only after its robots policy allows it.

        The TypeError fallback keeps injected test clients compatible with the
        older ``extract(url=...)`` contract.
        """
        extractor = self.server.competitor_content_client.extract
        try:
            return extractor(url=url, respect_robots=True)
        except TypeError as error:
            if "respect_robots" not in str(error):
                raise
            return extractor(url=url)

    @staticmethod
    def _is_adjacent_comparison_decision(*, selected_title: str, result: Mapping[str, Any], page: Mapping[str, Any]) -> bool:
        """Allow a real adjacent-fixture guide for the same comparison decision.

        This is intentionally a narrow recovery rule, not a replacement for
        the AI relevance gate.  It only applies to explicit comparison titles
        and rejects commerce paths, thin text, and pages that do not discuss
        both sides of the power-source trade-off.
        """
        title = " ".join(selected_title.casefold().split())
        if not any(marker in title for marker in (" vs ", " vs. ", " versus ", "compare", "comparison")):
            return False
        text = " ".join(
            str(value or "")
            for value in (result.get("title"), page.get("title"), page.get("content"))
        ).casefold()
        url = str(result.get("url") or "").casefold()
        if len(str(page.get("content") or "").strip()) < 900:
            return False
        if any(marker in url for marker in ("/product", "/products/", "/shop", "/store", "/category", "/collections/", "/cart")):
            return False
        has_power_tradeoff = "solar" in text and any(marker in text for marker in ("wired", "hardwired", "mains-powered", "mains powered"))
        has_fixture_context = "light" in text and any(marker in text for marker in ("outdoor", "landscape", "deck", "pathway", "path light", "stair"))
        has_editorial_shape = any(marker in text for marker in (" vs ", "versus", "comparison", "compare", "guide", "which is", "pros and cons", "advantages"))
        return has_power_tradeoff and has_fixture_context and has_editorial_shape

    def _upsert_competitor_memory(self, connection: sqlite3.Connection, project_id: int, result: Mapping[str, Any], page: Mapping[str, str]) -> int:
        with connection:
            write = self.server.collection_service.persist_content(
                connection,
                project_id=project_id,
                result=result,
                page=page,
                chunks=self._content_chunks,
            )
        return write.memory_id

    @staticmethod
    def _upsert_competitor_url_catalog(
        connection: sqlite3.Connection,
        project_id: int,
        result: Mapping[str, Any],
        *,
        collection_status: str,
        reason: str = "",
        memory_id: int | None = None,
    ) -> None:
        """Persist every discovered URL; bodies are linked only when allowed."""
        with connection:
            CollectionService().register_catalog_result(
                connection,
                project_id=project_id,
                result=result,
                collection_status=collection_status,
                reason=reason,
                memory_id=memory_id,
            )

    @staticmethod
    def _queued_competitor_catalog_candidates(
        connection: sqlite3.Connection,
        project_id: int,
        query: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT url,domain,search_title,last_rank,last_query
               FROM competitor_url_catalog
               WHERE project_id=? AND collection_status='queued' AND last_query=?
               ORDER BY last_rank ASC,id ASC""",
            (project_id, query),
        ).fetchall()
        return [
            {
                "url": str(row["url"]),
                "domain": str(row["domain"]),
                "title": str(row["search_title"]),
                "rank": int(row["last_rank"] or 0),
                "search_query": str(row["last_query"]),
            }
            for row in rows
        ]

    @staticmethod
    def _archive_robots_blocked_url(connection: sqlite3.Connection, project_id: int, result: Mapping[str, Any], error: Exception) -> None:
        """Keep the discovery URL and failure reason without retaining inaccessible text."""
        message = str(error).strip()
        if "robot" not in message.casefold():
            return
        url = str(result.get("url") or "").strip()
        normalized = normalize_competitor_url(url)
        if not normalized:
            return
        with connection:
            connection.execute(
                """INSERT INTO competitor_url_archive(project_id,normalized_url,url,domain,search_title,status,last_rank,last_query,error_summary)
                   VALUES(?,?,?,?,?,'robots_blocked',?,?,?)
                   ON CONFLICT(project_id,normalized_url,status) DO UPDATE SET
                     url=excluded.url,domain=excluded.domain,search_title=excluded.search_title,
                     last_rank=excluded.last_rank,last_query=excluded.last_query,error_summary=excluded.error_summary,
                     discovered_count=competitor_url_archive.discovered_count+1,last_seen_at=CURRENT_TIMESTAMP""",
                (
                    project_id, normalized, url, str(result.get("domain") or ""), str(result.get("title") or ""),
                    int(result.get("rank") or 0) or None, str(result.get("search_query") or ""), message[:1200],
                ),
            )

    @staticmethod
    def _content_chunks(content: str, size: int = 2400) -> list[str]:
        return [content[index:index + size] for index in range(0, len(content), size)] or [content]

    def _latest_competitor_research(self, connection: sqlite3.Connection, asset_id: int) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM competitor_research_runs WHERE content_asset_id=? ORDER BY id DESC LIMIT 1", (asset_id,)).fetchone()

    def _content_competitor_learning_gate(self, connection: sqlite3.Connection, asset: sqlite3.Row) -> dict[str, Any]:
        """Return the durable prerequisite state for an article's writing stages.

        A research run is considered learned only after it has persisted at least
        one readable competitor page *and* the model's structural analysis.  The
        check is deliberately tied to the content asset, rather than to all
        project memories, so a previous article can never unlock a new title.
        """
        research = self._latest_competitor_research(connection, int(asset["id"]))
        if research is None:
            return {
                "collected_count": 0, "analysis_ready": False, "learning_ready": False,
                "can_generate_outline": False,
                "blocked_reason": "请先采集并学习同行文章；完成后才能生成文章大纲。",
            }
        selected_count = int(connection.execute(
            "SELECT COUNT(*) AS count FROM competitor_research_items WHERE research_run_id=? AND status='selected'",
            (research["id"],),
        ).fetchone()["count"])
        try:
            analysis = json.loads(research["analysis_json"] or "{}")
        except json.JSONDecodeError:
            analysis = {}
        analysis_ready = isinstance(analysis, Mapping) and bool(analysis)
        learning_ready = research["status"] == "completed" and selected_count > 0 and analysis_ready
        if learning_ready:
            reason = ""
        elif research["status"] in {"running"}:
            reason = "同行文章正在采集和学习，请完成后再生成文章大纲。"
        elif research["status"] == "insufficient":
            reason = "本次未采集到可用同行文章；请调整标题或稍后重新采集后再生成大纲。"
        else:
            reason = "同行文章尚未完成学习；请先完成采集与分析后再生成文章大纲。"
        return {
            "research_run_id": int(research["id"]),
            "research_status": str(research["status"]),
            "collected_count": selected_count,
            "usable_count": int(research["usable_count"] or 0),
            "analysis_ready": analysis_ready,
            "learning_ready": learning_ready,
            "can_generate_outline": learning_ready,
            "blocked_reason": reason,
        }

    def _require_content_competitor_learning(self, connection: sqlite3.Connection, asset: sqlite3.Row) -> None:
        gate = self._content_competitor_learning_gate(connection, asset)
        if not gate["can_generate_outline"]:
            raise ValueError(str(gate["blocked_reason"]))

    def _competitor_research_payload(self, connection: sqlite3.Connection, run_id: int) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM competitor_research_runs WHERE id=?", (run_id,)).fetchone()
        if row is None: raise ValueError("competitor research does not exist")
        value = dict(row); value["analysis"] = json.loads(value.pop("analysis_json") or "{}")
        items = connection.execute("SELECT items.*,memory.page_title,memory.last_captured_at FROM competitor_research_items items LEFT JOIN competitor_content_memory memory ON memory.id=items.memory_id WHERE research_run_id=? ORDER BY rank,id", (run_id,)).fetchall()
        value["items"] = [dict(item) for item in items]
        return value

    def _research_sources(self, connection: sqlite3.Connection, asset_id: int) -> tuple[list[dict[str, Any]], Mapping[str, Any] | None]:
        research = self._latest_competitor_research(connection, asset_id)
        if research is None or research["status"] != "completed": return [], None
        rows = connection.execute("SELECT memory.* FROM competitor_research_items items JOIN competitor_content_memory memory ON memory.id=items.memory_id WHERE items.research_run_id=? AND items.status='selected' ORDER BY items.rank", (research["id"],)).fetchall()
        sources = [{"source_id": f"competitor-{item['id']}", "source_type": "competitor_page", "availability": "available", "url": item["url"], "publisher": item["domain"], "title": item["page_title"], "content": item["content"]} for item in rows]
        analysis = json.loads(research["analysis_json"] or "{}")
        return sources, analysis if isinstance(analysis, Mapping) else None

    def _list_content_memory(self) -> None:
        values = parse_qs(urlsplit(self.path).query).get("project_id", [])
        if len(values) != 1: self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id is required."}); return
        query = parse_qs(urlsplit(self.path).query).get("q", [""])[0].strip()
        with self._database() as connection:
            sql = "SELECT id,project_id,url,domain,page_title,structure_json,first_captured_at,last_captured_at FROM competitor_content_memory WHERE project_id=?"
            args: list[Any] = [int(values[0])]
            if query:
                sql += " AND (page_title LIKE ? OR content LIKE ?)"; args.extend([f"%{query}%", f"%{query}%"])
            rows = connection.execute(sql + " ORDER BY last_captured_at DESC", args).fetchall()
        self._json(HTTPStatus.OK, [{**dict(row), "structure": json.loads(row["structure_json"] or "{}") } for row in rows])

    def _list_competitor_url_archive(self) -> None:
        project_id = self._query_project_id()
        if project_id is None:
            return
        try:
            with self._database() as connection:
                self._project_exists(connection, project_id)
                rows = connection.execute(
                    """SELECT id,url,domain,search_title,status,last_rank,last_query,error_summary,
                              discovered_count,first_seen_at,last_seen_at
                       FROM competitor_url_archive WHERE project_id=?
                       ORDER BY last_seen_at DESC,id DESC""",
                    (project_id,),
                ).fetchall()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, [dict(row) for row in rows])

    def _list_competitor_url_catalog(self) -> None:
        project_id = self._query_project_id()
        if project_id is None:
            return
        try:
            rows = self.server.collection_repository.list_catalog(project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, rows)

    def _list_collection_plans(self) -> None:
        project_id = self._query_project_id()
        if project_id is None:
            return
        try:
            rows = self.server.collection_repository.list_collection_plans(project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, rows)

    def _save_collection_plan(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        source_type = self._optional_text(payload, "source_type") or "keyword"
        source_value = self._optional_text(payload, "source_value")
        if not source_value:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "source_value is required."})
            return
        settings = payload.get("settings")
        if settings is not None and not isinstance(settings, Mapping):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "settings must be an object."})
            return
        try:
            with self._database() as connection, connection:
                value = self.server.collection_service.save_plan(
                    connection,
                    project_id=project_id,
                    source_type=source_type,
                    source_value=source_value,
                    enabled=payload.get("enabled") is not False,
                    schedule=str(payload.get("schedule") or "manual"),
                    settings=settings,
                )
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, value)

    def _discover_collection_plan(self, plan_id: int, payload: Mapping[str, Any]) -> None:
        """Execute a domain or keyword plan into the unified URL catalog."""
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            max_results = min(self._limit(payload, "max_results", 30), 100)
            locale = self._optional_text(payload, "locale") or "en-US"
            with self._database() as connection:
                plan = self.server.collection_service.get_plan(
                    connection, project_id=project_id, plan_id=plan_id
                )
                if plan["status"] != "active":
                    raise ValueError("collection plan is paused")
                if plan["source_type"] == "first_party":
                    raise ValueError("first_party plans use the project website-crawl collector, not competitor discovery")
                source_value = str(plan["source_value"])
                if plan["source_type"] == "domain":
                    parsed = urlparse(source_value if "://" in source_value else f"https://{source_value}")
                    domain = (parsed.hostname or "").removeprefix("www.")
                    if not domain:
                        raise ValueError("domain collection plan has an invalid source_value")
                    query = f"site:{domain}"
                else:
                    query = source_value

            search_client = getattr(self.server, "competitor_search_client", None)
            if search_client is None:
                serper_key = _serper_api_key(self.server.ai_settings_path)
                search_client = SerperSearchClient(serper_key) if serper_key else self.server.competitor_content_client
            results = search_client.search(query=query, locale=locale, max_results=max_results)
            queued = excluded = 0
            with self._database() as connection, connection:
                # Recheck plan ownership after network I/O before writing.
                self.server.collection_service.get_plan(connection, project_id=project_id, plan_id=plan_id)
                own_domain = self._project_domain(connection, project_id)
                for rank, item in enumerate(results, 1):
                    result = dict(item)
                    result["rank"] = int(result.get("rank") or rank)
                    result["search_query"] = query
                    reason = competitor_candidate_exclusion_reason(result, own_domain)
                    self.server.collection_service.register_catalog_result(
                        connection,
                        project_id=project_id,
                        result=result,
                        collection_status="excluded" if reason else "queued",
                        reason=reason or "",
                    )
                    if reason:
                        excluded += 1
                    else:
                        queued += 1
                connection.execute(
                    """UPDATE collection_plans SET discovered_count=discovered_count+?,
                           last_discovered_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND project_id=?""",
                    (len(results), plan_id, project_id),
                )
        except (sqlite3.Error, ValueError, CompetitorContentProtocolError, SerperSearchProtocolError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {
            "plan_id": plan_id,
            "project_id": project_id,
            "query": query,
            "discovered_count": len(results),
            "queued_count": queued,
            "excluded_count": excluded,
        })

    def _list_competitor_catalog_collection_run_items(self, run_id: int) -> None:
        project_id = self._query_project_id()
        if project_id is None:
            return
        try:
            with self._database() as connection:
                rows = self.server.collection_service.list_run_items(
                    connection, project_id=project_id, run_id=run_id
                )
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, rows)

    def _list_competitor_catalog_collection_runs(self) -> None:
        project_id = self._query_project_id()
        if project_id is None:
            return
        try:
            with self._database() as connection:
                self._project_exists(connection, project_id)
                rows = connection.execute(
                    """SELECT id,project_id,status,total_count,collected_count,unchanged_count,already_collected_count,
                              robots_blocked_count,failed_count,recovered_count,last_heartbeat_at,
                              error_summary,started_at,completed_at,created_at,updated_at
                       FROM competitor_catalog_collection_runs WHERE project_id=?
                       ORDER BY id DESC LIMIT 20""",
                    (project_id,),
                ).fetchall()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, [dict(row) for row in rows])

    def _queue_competitor_catalog_collection(self, payload: Mapping[str, Any]) -> None:
        """Queue one compliant, URL-checkpointed pass over uncollected pages."""
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection, connection:
                row, candidate_ids = self.server.collection_service.create_catalog_run(
                    connection, project_id=project_id
                )
                run_id = int(row["id"])
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if candidate_ids:
            self.server.enqueue_background_task("competitor_catalog_collection", project_id, run_id)
            self._json(HTTPStatus.ACCEPTED, row)
            return
        self._json(HTTPStatus.OK, row)

    @staticmethod
    def _mark_competitor_catalog_entries(
        connection: sqlite3.Connection,
        project_id: int,
        canonical_url: str,
        *,
        status: str,
        memory_id: int | None = None,
        reason: str = "",
    ) -> None:
        """Compatibility wrapper for the unified collection persistence service."""
        CollectionService.mark_catalog(
            connection,
            project_id=project_id,
            normalized_url=canonical_url,
            status=status,
            memory_id=memory_id,
            reason=reason,
        )

    def _execute_competitor_catalog_collection(self, run_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection, connection:
                claimed = self.server.collection_service.claim_run_items(
                    connection, project_id=project_id, run_id=run_id
                )
                if not claimed:
                    row = self.server.collection_service.recalculate_run(
                        connection, project_id=project_id, run_id=run_id, complete_if_idle=True
                    )
                    self._json(HTTPStatus.OK, row)
                    return
        except (sqlite3.Error, ValueError) as error:
            self._finish_competitor_catalog_collection_failure(run_id, project_id, error)
            return

        candidates = [{
            "item_id": int(row["id"]),
            "url": str(row["source_url"]),
            "domain": str(row.get("domain") or ""),
            "title": str(row.get("search_title") or ""),
            "rank": int(row.get("last_rank") or 0),
            "search_query": str(row.get("last_query") or ""),
        } for row in claimed]
        try:
            batch = (
                self.server.competitor_content_client.extract_many(
                    [item["url"] for item in candidates], max_workers=5, respect_robots=True
                )
                if callable(getattr(self.server.competitor_content_client, "extract_many", None))
                else {}
            )
        except Exception:
            # A batch-adapter failure falls back to isolated URL requests so
            # one process-level error cannot erase every item's checkpoint.
            batch = {}
        try:
            for item in candidates:
                canonical = normalize_competitor_url(item["url"])
                try:
                    fetched = batch.get(item["url"]) if batch else None
                    if isinstance(fetched, Exception):
                        raise fetched
                    page = fetched if isinstance(fetched, Mapping) else self._extract_competitor_content(url=item["url"])
                    if not isinstance(page, Mapping):
                        raise CompetitorContentProtocolError("Competitor extraction returned no article content.")
                    with self._database() as connection, connection:
                        write = self.server.collection_service.persist_content(
                            connection,
                            project_id=project_id,
                            result=item,
                            page=page,
                            chunks=self._content_chunks,
                            extractor=str(page.get("extractor") or "competitor_content_client"),
                        )
                        self._mark_competitor_catalog_entries(
                            connection, project_id, canonical, status="collected", memory_id=write.memory_id
                        )
                        self.server.collection_service.finish_item(
                            connection,
                            project_id=project_id,
                            run_id=run_id,
                            item_id=int(item["item_id"]),
                            status=write.outcome,
                            memory_id=write.memory_id,
                            version_id=write.version_id,
                            extractor=str(page.get("extractor") or "competitor_content_client"),
                        )
                except Exception as error:
                    blocked = "robot" in str(error).casefold()
                    with self._database() as connection, connection:
                        if blocked:
                            self._archive_robots_blocked_url(connection, project_id, item, error)
                        self._mark_competitor_catalog_entries(
                            connection, project_id, canonical,
                            status="robots_blocked" if blocked else "failed", reason=str(error),
                        )
                        self.server.collection_service.finish_item(
                            connection,
                            project_id=project_id,
                            run_id=run_id,
                            item_id=int(item["item_id"]),
                            status="robots_blocked" if blocked else "failed",
                            error_summary=str(error),
                        )
            with self._database() as connection, connection:
                row = self.server.collection_service.recalculate_run(
                    connection, project_id=project_id, run_id=run_id, complete_if_idle=True
                )
        except Exception as error:
            self._finish_competitor_catalog_collection_failure(run_id, project_id, error)
            return
        self._json(HTTPStatus.OK, row)

    def _finish_competitor_catalog_collection_failure(self, run_id: int, project_id: int, error: Exception) -> None:
        with self._database() as connection, connection:
            connection.execute(
                """UPDATE competitor_catalog_collection_runs
                   SET status='failed',error_summary=?,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND project_id=?""",
                (str(error)[:1200], run_id, project_id),
            )
            row = connection.execute(
                "SELECT * FROM competitor_catalog_collection_runs WHERE id=? AND project_id=?",
                (run_id, project_id),
            ).fetchone()
        self._json(HTTPStatus.OK, dict(row) if row else {"id": run_id, "status": "failed"})

    def _list_competitor_content_learning_runs(self) -> None:
        project_id = self._query_project_id()
        if project_id is None:
            return
        try:
            with self._database() as connection:
                self._project_exists(connection, project_id)
                rows = connection.execute(
                    """SELECT id,project_id,status,source_count,processed_count,memories_created_count,
                              memories_updated_count,memories_rejected_count,
                              provider,model,error_summary,started_at,completed_at,created_at,updated_at
                       FROM competitor_content_learning_runs WHERE project_id=? ORDER BY id DESC LIMIT 20""",
                    (project_id,),
                ).fetchall()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, [dict(row) for row in rows])

    def _queue_collected_competitor_content_learning(self, payload: Mapping[str, Any]) -> None:
        """Queue AI strategy extraction from every readable page in this project's collection library."""
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        provider = self._optional_text(payload, "provider") or "deepseek"
        model = self._optional_text(payload, "model")
        if provider not in AI_PROVIDERS:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "provider must be openai, gemini, or deepseek"})
            return
        if model is not None and not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", model):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "model contains unsupported characters"})
            return
        try:
            with self._database() as connection, connection:
                self._project_exists(connection, project_id)
                active = connection.execute(
                    "SELECT 1 FROM competitor_content_learning_runs WHERE project_id=? AND status IN ('queued','running')",
                    (project_id,),
                ).fetchone()
                if active is not None:
                    raise ValueError("a collected-content learning run is already queued or running for this project")
                rows = connection.execute(
                    "SELECT id,url,content_hash FROM competitor_content_memory WHERE project_id=? AND trim(content)<>'' ORDER BY id ASC",
                    (project_id,),
                ).fetchall()
                candidate_ids: list[int] = []
                seen: set[str] = set()
                for row in rows:
                    canonical = normalize_competitor_url(str(row["url"]))
                    content_hash = str(row["content_hash"] or "")
                    key = canonical or content_hash
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    candidate_ids.append(int(row["id"]))
                status = "queued" if candidate_ids else "completed"
                cursor = connection.execute(
                    """INSERT INTO competitor_content_learning_runs(
                           project_id,status,candidate_memory_ids_json,source_count,provider,model,completed_at
                       ) VALUES(?,?,?,?,?,?,CASE WHEN ?='completed' THEN CURRENT_TIMESTAMP ELSE NULL END)""",
                    (project_id, status, json.dumps(candidate_ids), len(candidate_ids), provider, model, status),
                )
                run_id = int(cursor.lastrowid)
                row = connection.execute("SELECT * FROM competitor_content_learning_runs WHERE id=?", (run_id,)).fetchone()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if candidate_ids:
            self.server.enqueue_background_task("collected_competitor_learning", project_id, run_id)
            self._json(HTTPStatus.ACCEPTED, dict(row))
            return
        self._json(HTTPStatus.OK, dict(row))

    def _execute_collected_competitor_content_learning(self, run_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection, connection:
                self._project_exists(connection, project_id)
                run = connection.execute(
                    "SELECT * FROM competitor_content_learning_runs WHERE id=? AND project_id=?", (run_id, project_id)
                ).fetchone()
                if run is None:
                    raise ValueError("collected-content learning run does not exist in this project")
                if run["status"] != "queued":
                    self._json(HTTPStatus.OK, dict(run))
                    return
                candidate_ids = json.loads(run["candidate_memory_ids_json"] or "[]")
                if not isinstance(candidate_ids, list) or not all(isinstance(item, int) for item in candidate_ids):
                    raise ValueError("collected-content learning run has invalid candidates")
                connection.execute(
                    "UPDATE competitor_content_learning_runs SET status='running',started_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (run_id,),
                )
                placeholders = ",".join("?" for _ in candidate_ids)
                rows = connection.execute(
                    f"SELECT id,url,domain,page_title,content,content_hash FROM competitor_content_memory WHERE project_id=? AND id IN ({placeholders}) ORDER BY id ASC",
                    (project_id, *candidate_ids),
                ).fetchall() if candidate_ids else []
        except (sqlite3.Error, ValueError, json.JSONDecodeError) as error:
            self._finish_collected_competitor_content_learning_failure(run_id, project_id, error)
            return

        try:
            generator, provider, model = self._content_generator({"provider": str(run["provider"]), "model": run["model"]})
            if generator is None:
                raise ValueError(f"{self._content_provider_label(str(run['provider']))} must be configured before AI learning can run")
            processed = created = updated = rejected = 0
            for start in range(0, len(rows), 6):
                batch = rows[start:start + 6]
                sources = [
                    {
                        "source_id": f"competitor-memory-{item['id']}", "title": str(item["page_title"]),
                        "url": str(item["url"]), "domain": str(item["domain"]),
                        "content_excerpt": re.sub(r"\s+", " ", str(item["content"]))[:3600],
                    }
                    for item in batch
                ]
                if callable(getattr(generator, "run_stage", None)):
                    raw_cards = generator.run_stage(stage="competitor_memory_synthesis", data={"source_documents": sources, "policy": "learn_structure_and_method_only_no_competitor_prose"})
                elif callable(getattr(generator, "generate", None)):
                    raw_cards = generator.generate(stage="competitor_memory_synthesis", source_documents=sources, policy="learn_structure_and_method_only_no_competitor_prose")
                else:
                    raise ContentGenerationProtocolError("configured content generator has no supported stage method")
                result = json.loads(raw_cards) if isinstance(raw_cards, str) else raw_cards
                cards = result.get("memory_cards", []) if isinstance(result, Mapping) else []
                if not isinstance(cards, list):
                    raise ContentGenerationProtocolError("AI content competitor_memory_synthesis returned invalid memory cards")
                with self._database() as connection, connection:
                    valid_cards = [card for card in cards if isinstance(card, Mapping)]
                    learned = MemoryLearningService(connection, project_id=project_id).save_competitor_cards(
                        valid_cards,
                        source_documents=sources,
                        learning_run_id=run_id,
                    )
                    created += learned.created_count
                    updated += learned.updated_count
                    rejected += learned.rejected_count + (len(cards) - len(valid_cards))
                    processed += len(batch)
                    connection.execute(
                        """UPDATE competitor_content_learning_runs
                           SET processed_count=?,memories_created_count=?,memories_updated_count=?,
                               memories_rejected_count=?,provider=?,model=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (processed, created, updated, rejected, provider, model, run_id),
                    )
            with self._database() as connection, connection:
                connection.execute(
                    """UPDATE competitor_content_learning_runs
                       SET status='completed',processed_count=?,memories_created_count=?,memories_updated_count=?,
                           memories_rejected_count=?,provider=?,model=?,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (processed, created, updated, rejected, provider, model, run_id),
                )
                row = connection.execute("SELECT * FROM competitor_content_learning_runs WHERE id=?", (run_id,)).fetchone()
        except Exception as error:
            self._finish_collected_competitor_content_learning_failure(run_id, project_id, error)
            return
        self._json(HTTPStatus.OK, dict(row))

    def _finish_collected_competitor_content_learning_failure(self, run_id: int, project_id: int, error: Exception) -> None:
        with self._database() as connection, connection:
            connection.execute(
                """UPDATE competitor_content_learning_runs SET status='failed',error_summary=?,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND project_id=?""",
                (str(error)[:1200], run_id, project_id),
            )
            row = connection.execute("SELECT * FROM competitor_content_learning_runs WHERE id=?", (run_id,)).fetchone()
        self._json(HTTPStatus.OK, dict(row) if row else {"id": run_id, "status": "failed"})

    def _get_competitor_learning(self, project_id: int, *, include_runs: bool) -> None:
        try:
            with self._database() as connection:
                self._project_exists(connection, project_id)
                schedule = connection.execute("SELECT * FROM competitor_learning_schedules WHERE project_id=?", (project_id,)).fetchone()
                cards = connection.execute("SELECT * FROM competitor_style_cards WHERE project_id=? ORDER BY updated_at DESC,id DESC LIMIT 60", (project_id,)).fetchall()
                runs = connection.execute("SELECT * FROM competitor_learning_runs WHERE project_id=? ORDER BY id DESC LIMIT 30", (project_id,)).fetchall() if include_runs else []
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        schedule_value: dict[str, Any] = {"project_id": project_id, "topics": [], "interval_days": 14, "enabled": 0, "last_run_at": None, "next_run_at": None}
        if schedule is not None:
            schedule_value.update(dict(schedule))
            try: schedule_value["topics"] = json.loads(schedule_value.pop("topics_json") or "[]")
            except (TypeError, json.JSONDecodeError): schedule_value["topics"] = []
        self._json(HTTPStatus.OK, {"schedule": schedule_value, "cards": [self._competitor_style_card_payload(row) for row in cards], "runs": [dict(row) for row in runs]})

    @staticmethod
    def _competitor_style_card_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(row)
        try: value["evidence"] = json.loads(value.pop("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError): value["evidence"] = {}
        return value

    def _save_competitor_learning_schedule(self, project_id: int, payload: Mapping[str, Any]) -> None:
        raw_topics = payload.get("topics", [])
        if not isinstance(raw_topics, list) or not all(isinstance(item, str) for item in raw_topics):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "topics must be a list of topic strings."}); return
        topics = list(dict.fromkeys(" ".join(item.split())[:180] for item in raw_topics if item.strip()))
        if len(topics) > 8:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "at most 8 competitor learning topics are allowed."}); return
        interval = payload.get("interval_days", 14)
        enabled = payload.get("enabled", False)
        if not isinstance(interval, int) or isinstance(interval, bool) or not 7 <= interval <= 90:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "interval_days must be an integer from 7 to 90."}); return
        if not isinstance(enabled, bool):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "enabled must be true or false."}); return
        provider = self._optional_text(payload, "provider") or "deepseek"
        model = self._optional_text(payload, "model")
        if provider is not None and provider not in AI_PROVIDERS:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "provider must be openai, gemini, or deepseek."}); return
        try:
            with self._database() as connection, connection:
                self._project_exists(connection, project_id)
                connection.execute(
                    """INSERT INTO competitor_learning_schedules(project_id,topics_json,interval_days,enabled,provider,model,next_run_at)
                       VALUES(?,?,?,?,?,?,CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)
                       ON CONFLICT(project_id) DO UPDATE SET topics_json=excluded.topics_json,interval_days=excluded.interval_days,
                         enabled=excluded.enabled,provider=excluded.provider,model=excluded.model,
                         next_run_at=CASE WHEN excluded.enabled=0 THEN NULL WHEN competitor_learning_schedules.enabled=0 THEN CAST(CURRENT_TIMESTAMP AS TEXT) ELSE competitor_learning_schedules.next_run_at END,
                         updated_at=CURRENT_TIMESTAMP""",
                    (project_id, json.dumps(topics, ensure_ascii=False), interval, int(enabled), provider, model, int(enabled)),
                )
                row = connection.execute("SELECT * FROM competitor_learning_schedules WHERE project_id=?", (project_id,)).fetchone()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        value = dict(row)
        value["topics"] = json.loads(value.pop("topics_json") or "[]")
        self.server.periodic_learning_wake.set()
        self._json(HTTPStatus.OK, value)

    def _queue_competitor_learning_run(self, project_id: int, payload: Mapping[str, Any], *, trigger_type: str) -> None:
        requested_topic = self._optional_text(payload, "topic")
        try:
            with self._database() as connection, connection:
                self._project_exists(connection, project_id)
                schedule = connection.execute("SELECT * FROM competitor_learning_schedules WHERE project_id=?", (project_id,)).fetchone()
                if schedule is None:
                    raise ValueError("save a competitor learning schedule before running it")
                topics = json.loads(schedule["topics_json"] or "[]")
                topic = requested_topic or (topics[0] if topics else "")
                if not isinstance(topic, str) or not topic.strip():
                    raise ValueError("add at least one learning topic or provide topic")
                running = connection.execute("SELECT 1 FROM competitor_learning_runs WHERE project_id=? AND status IN ('queued','running')", (project_id,)).fetchone()
                if running is not None:
                    raise ValueError("a competitor learning run is already queued or running for this project")
                cursor = connection.execute("INSERT INTO competitor_learning_runs(project_id,schedule_id,topic,trigger_type) VALUES(?,?,?,?)", (project_id, schedule["id"], topic[:300], trigger_type))
                run_id = int(cursor.lastrowid)
                row = connection.execute("SELECT * FROM competitor_learning_runs WHERE id=?", (run_id,)).fetchone()
        except (sqlite3.Error, ValueError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self.server.enqueue_background_task("competitor_learning", project_id, run_id)
        self._json(HTTPStatus.ACCEPTED, dict(row))

    def _execute_competitor_learning_run(self, project_id: int, run_id: int) -> None:
        try:
            with self._database() as connection, connection:
                run = connection.execute("SELECT * FROM competitor_learning_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
                if run is None: raise ValueError("competitor learning run does not exist in this project")
                if run["status"] != "queued":
                    self._json(HTTPStatus.OK, dict(run)); return
                connection.execute("UPDATE competitor_learning_runs SET status='running',started_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
                schedule = connection.execute("SELECT * FROM competitor_learning_schedules WHERE id=?", (run["schedule_id"],)).fetchone()
                asset = self._competitor_learning_asset(connection, project_id, str(run["topic"]))
                if asset is None:
                    raise ValueError("no content title matches this learning topic; create or select a related title first")
                provider_payload = {"provider": schedule["provider"], "model": schedule["model"]} if schedule is not None else {}
                generator, provider, model = self._content_generator(provider_payload)
                if generator is None: raise ValueError("selected competitor learning model is not configured")
                research = self._run_competitor_research(connection, asset, generator, provider, model)
                cards_created = self._save_competitor_style_cards(connection, project_id, run, research)
                source_count = int(research.get("usable_count") or 0)
                connection.execute("UPDATE competitor_learning_runs SET content_asset_id=?,status='completed',source_count=?,cards_created=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (asset["id"], source_count, cards_created, run_id))
                if schedule is not None:
                    connection.execute("UPDATE competitor_learning_schedules SET last_run_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (schedule["id"],))
                result = connection.execute("SELECT * FROM competitor_learning_runs WHERE id=?", (run_id,)).fetchone()
        except (sqlite3.Error, ValueError, ContentGenerationProtocolError, CompetitorContentProtocolError) as error:
            status = "insufficient" if isinstance(error, CompetitorContentProtocolError) else "failed"
            with self._database() as connection, connection:
                connection.execute("UPDATE competitor_learning_runs SET status=?,error_summary=?,completed_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?", (status, str(error)[:1200], run_id, project_id))
                result = connection.execute("SELECT * FROM competitor_learning_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
            self._json(HTTPStatus.OK, dict(result) if result else {"id": run_id, "status": status, "error_summary": str(error)[:1200]}); return
        self._json(HTTPStatus.OK, dict(result))

    @staticmethod
    def _competitor_learning_asset(connection: sqlite3.Connection, project_id: int, topic: str) -> sqlite3.Row | None:
        rows = connection.execute("""SELECT assets.*,keywords.keyword FROM content_assets assets JOIN keywords ON keywords.id=assets.keyword_id
            WHERE assets.project_id=? AND assets.deleted_at IS NULL ORDER BY assets.updated_at DESC,assets.id DESC""", (project_id,)).fetchall()
        topic_terms = {term.casefold() for term in re.findall(r"[A-Za-z0-9]{3,}", topic)}
        if not rows: return None
        if not topic_terms: return rows[0]
        best = max(rows, key=lambda row: len(topic_terms & {term.casefold() for term in re.findall(r"[A-Za-z0-9]{3,}", f"{row['title_snapshot']} {row['keyword'] or ''}")}))
        terms = {term.casefold() for term in re.findall(r"[A-Za-z0-9]{3,}", f"{best['title_snapshot']} {best['keyword'] or ''}")}
        return best if topic_terms & terms else None

    def _save_competitor_style_cards(self, connection: sqlite3.Connection, project_id: int, run: sqlite3.Row, research: Mapping[str, Any]) -> int:
        analysis = research.get("analysis") if isinstance(research.get("analysis"), Mapping) else {}
        patterns = [" ".join(str(item).split())[:1200] for item in analysis.get("writing_patterns", []) if isinstance(item, str) and item.strip()]
        outline = analysis.get("dynamic_outline", [])
        headings = [str(item.get("heading") or "").strip() for item in outline if isinstance(item, Mapping) and str(item.get("heading") or "").strip()]
        cards: list[tuple[str, str]] = [(f"竞品写法策略 {index}", pattern) for index, pattern in enumerate(patterns[:3], 1)]
        if headings:
            cards.append(("覆盖顺序与信息格式", "按读者决策顺序覆盖：" + " → ".join(headings[:8]) + "。仅学习结构安排，不复用竞品表达或事实。"))
        if not cards:
            cards.append(("竞品结构观察", "本次只保留可验证的页面结构、决策顺序和信息格式；没有足够稳定的模式时，不写入具体写法结论。"))
        sources = [{"url": item.get("url"), "title": item.get("page_title") or item.get("search_title"), "domain": item.get("domain")} for item in research.get("items", []) if isinstance(item, Mapping) and item.get("status") == "selected"]
        signature = hashlib.sha256(json.dumps(sorted(str(item.get("url") or "") for item in sources), ensure_ascii=False).encode("utf-8")).hexdigest()
        quality = min(0.9, 0.45 + min(len(sources), 5) * 0.08 + min(len(patterns), 3) * 0.03)
        for title, summary in cards:
            evidence = {"source": "periodic_competitor_learning", "learning_run_id": run["id"], "source_count": len(sources), "sources": sources, "policy": "structure_and_method_only_no_competitor_prose"}
            source_hash = f"competitor-style:{signature}:{hashlib.sha256(title.encode()).hexdigest()[:12]}"
            connection.execute("""INSERT INTO competitor_style_cards(project_id,schedule_id,learning_run_id,topic,card_title,summary,evidence_json,source_signature,quality_score)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id,source_signature,card_title) DO UPDATE SET learning_run_id=excluded.learning_run_id,topic=excluded.topic,summary=excluded.summary,evidence_json=excluded.evidence_json,quality_score=excluded.quality_score,updated_at=CURRENT_TIMESTAMP""", (project_id, run["schedule_id"], run["id"], run["topic"], title, summary, json.dumps(evidence, ensure_ascii=False), signature, quality))
            connection.execute("""INSERT INTO content_learning_memories(project_id,memory_type,topic,summary,evidence_json,source_url,source_content_hash,quality_score,status)
                VALUES(?,?,?,?,?,?,?,?, 'active') ON CONFLICT(project_id,source_content_hash) DO UPDATE SET topic=excluded.topic,summary=excluded.summary,evidence_json=excluded.evidence_json,source_url=excluded.source_url,quality_score=excluded.quality_score,status='active',updated_at=CURRENT_TIMESTAMP""", (project_id, "style", str(run["topic"])[:300], summary, json.dumps(evidence, ensure_ascii=False), str(sources[0].get("url") or "") if sources else "", source_hash, quality))
        return len(cards)

    def _list_authority_sources(self) -> None:
        values = parse_qs(urlsplit(self.path).query).get("project_id", [])
        if len(values) != 1:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id is required."}); return
        with self._database() as connection:
            rows = connection.execute("SELECT * FROM authority_source_library WHERE project_id=? ORDER BY updated_at DESC,id DESC", (int(values[0]),)).fetchall()
        self._json(HTTPStatus.OK, [self._authority_source_payload(row) for row in rows])

    def _create_authority_source(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        title, content = self._text(payload, "title"), self._text(payload, "content")
        source_type = self._optional_text(payload, "source_type") or "first_party"
        if project_id is None or title is None or content is None:
            return
        if source_type not in {"first_party", "standard", "certification", "government", "industry_research"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid authority source type."}); return
        try:
            with self._database() as connection:
                if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                    raise ValueError("project does not exist")
                # Authority-library intake is knowledge organization, not article
                # writing. Keep it independent from the GPT writer route.
                generator, provider, model = self._content_generator({**payload, "provider": "deepseek"})
                classification: Mapping[str, Any] = {}
                if generator is not None:
                    data = {"title": title, "source_type": source_type, "url": self._optional_text(payload, "url") or "", "publisher": self._optional_text(payload, "publisher") or "", "published_at": self._optional_text(payload, "published_at") or "", "content": content[:30000]}
                    raw = generator.run_stage(stage="source_classification", data=data) if callable(getattr(generator, "run_stage", None)) else generator.generate(stage="source_classification", **data)
                    value = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(value, Mapping): classification = value
                authority = classification.get("authority_level") if isinstance(classification.get("authority_level"), str) else "needs_review"
                if authority not in {"primary", "authoritative", "supporting", "needs_review"}: authority = "needs_review"
                tags = classification.get("tags") if isinstance(classification.get("tags"), list) else []
                with connection:
                    cursor = connection.execute("INSERT INTO authority_source_library(project_id,title,source_type,url,publisher,published_at,content,authority_level,tags_json,classification_json,summary) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (project_id, title, source_type, self._optional_text(payload, "url"), self._optional_text(payload, "publisher"), self._optional_text(payload, "published_at"), content, authority, json.dumps([item for item in tags if isinstance(item, str)], ensure_ascii=False), json.dumps(dict(classification), ensure_ascii=False), classification.get("summary") if isinstance(classification.get("summary"), str) else None))
                    row = connection.execute("SELECT * FROM authority_source_library WHERE id=?", (cursor.lastrowid,)).fetchone()
        except (sqlite3.Error, ValueError, ContentGenerationProtocolError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.CREATED, self._authority_source_payload(row))

    def _delete_authority_source(self, source_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None: return
        with self._database() as connection:
            # The PostgreSQL runtime commits only inside the connection context.
            # Without this block DELETE returned 200 but was rolled back when the
            # request connection closed, so the source reappeared after refresh.
            with connection:
                cursor = connection.execute("DELETE FROM authority_source_library WHERE id=? AND project_id=?", (source_id, project_id))
        if cursor.rowcount != 1:
            self._json(HTTPStatus.NOT_FOUND, {"error": "authority source does not exist in this website."}); return
        self._json(HTTPStatus.OK, {"deleted": 1})

    def _research_authority_sources(self, payload: Mapping[str, Any]) -> None:
        """Find citations from allowlisted search results, never AI-invented URLs."""
        # Authority-link research deliberately has its own locked model.  The
        # UI sends Gemini here: it proposes candidate URLs, then receives the
        # fetched page back as an independent accept/reject verification step.
        # It must never silently fall back to a different model or to Serper.
        if self._optional_text(payload, "provider") == "gemini":
            self._research_authority_sources_ai_legacy(payload)
            return
        project_id = self._integer(payload, "project_id")
        asset_id = self._integer(payload, "asset_id") if "asset_id" in payload else None
        if project_id is None or asset_id is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id and a completed article asset_id are required."}); return
        search_run_id = uuid.uuid4().hex
        try:
            with self._database() as connection:
                asset = self._content_asset(connection, project_id, asset_id)
                draft = connection.execute("SELECT markdown FROM content_drafts WHERE id=?", (asset["current_draft_id"],)).fetchone() if asset["current_draft_id"] else None
                if draft is None: raise ValueError("a completed article is required before researching authority sources")
                # The reader shows only the current run. Persistent exclusion
                # memory is stored separately, so clearing the audit does not
                # cause known-bad URLs to be retried later.
                with connection:
                    connection.execute("DELETE FROM authority_search_results WHERE project_id=? AND content_asset_id=?", (project_id, asset_id))
                    # A new research run replaces this article's citation set.
                    # Keep the source-library record as website memory, but do
                    # not leave an old, now-irrelevant source displayed as a
                    # citation for the newly researched article.
                    connection.execute("DELETE FROM content_authority_source_links WHERE project_id=? AND content_asset_id=?", (project_id, asset_id))
                candidates: list[dict[str, str]] = []
                max_authority_candidates = 8
                max_audit_rows = 8
                skipped: list[dict[str, str]] = []
                seen_urls: set[str] = set()
                prior_unusable = self._previously_unusable_authority_urls(connection, project_id)
                audit_rows: list[dict[str, str]] = []
                serper_key = _serper_api_key(self.server.ai_settings_path)
                serper_client = SerperSearchClient(serper_key) if serper_key else None

                def audit_skip(task: Mapping[str, str], *, url: str, title: str, domain: str, rank: int, reason: str) -> None:
                    self._remember_authority_exclusion(connection, project_id, url, reason)
                    if len(audit_rows) >= max_audit_rows:
                        return
                    skipped.append({"url": url, "title": title, "reason": reason})
                    audit_rows.append({"section_heading": task["section_heading"], "claim_topic": task["claim_topic"], "query": task["query"], "url": url, "title": title, "domain": domain, "rank": str(rank), "status": "skipped", "reason": reason})

                def add_results(task: Mapping[str, str], results: list[Mapping[str, Any]], *, provider: str) -> None:
                    for result in results:
                        url = str(result.get("url") or "")
                        domain = str(result.get("domain") or "")
                        normalized = self._normalized_authority_url(url)
                        rank = int(result.get("rank") or 0)
                        title = str(result.get("title") or "")
                        if not url or not normalized or normalized in seen_urls:
                            continue
                        seen_urls.add(normalized)
                        if not self._authority_domain_allowed(domain):
                            continue
                        if self._authority_download_url(url):
                            audit_skip(task, url=url, title=title, domain=domain, rank=rank, reason="Skipped before opening: downloadable files such as PDF, Office, spreadsheet, archive, and CSV are not valid citation pages.")
                            continue
                        if self._authority_non_article_url(url):
                            audit_skip(task, url=url, title=title, domain=domain, rank=rank, reason="Skipped before opening: this is a document viewer, public-records portal, bid attachment, or download route rather than a citable article page.")
                            continue
                        relevant, relevance_reason = self._authority_source_relevance(
                            keyword=str(asset["keyword"]),
                            article_title=str(asset["title_snapshot"]),
                            section_heading=task["section_heading"],
                            source_title=title,
                        )
                        if not relevant:
                            audit_skip(task, url=url, title=title, domain=domain, rank=rank, reason=f"Skipped before opening: unrelated to this article section. {relevance_reason}")
                            continue
                        if normalized in prior_unusable:
                            audit_skip(task, url=url, title=title, domain=domain, rank=rank, reason="Skipped before opening: this URL was previously unreadable, blocked by robots, or not a usable HTML content page.")
                            continue
                        candidates.append({"url": url, "title": title, "domain": domain, "section_heading": task["section_heading"], "claim_topic": task["claim_topic"], "query": task["query"], "rank": str(rank), "provider": provider})
                        if len(candidates) >= max_authority_candidates:
                            return

                for task in self._authority_google_tasks(str(draft["markdown"]), asset["title_snapshot"], asset["keyword"]):
                    try:
                        if serper_client is not None:
                            results = serper_client.search(query=task["query"], locale=asset["locale"], max_results=10)
                            search_provider = "serper"
                        else:
                            results = self.server.competitor_content_client.search(query=task["query"], locale=asset["locale"], max_results=10)
                            search_provider = "google"
                    except (GoogleSerpProtocolError, SerperSearchProtocolError) as error:
                        search_name = "Serper" if serper_client is not None else "Google restricted"
                        reason = f"{search_name} search failed: {error}"
                        audit_rows.append({"section_heading": task["section_heading"], "claim_topic": task["claim_topic"], "query": task["query"], "url": "", "title": task["section_heading"], "domain": "", "rank": "0", "status": "search_error", "reason": reason})
                        continue
                    add_results(task, results, provider=search_provider)
                    if len(candidates) >= max_authority_candidates:
                        break

                # Google frequently returns government PDFs rather than readable pages.
                # Bing is a tested fallback, queried once per allowed domain family because
                # its single-query OR site syntax is not reliable.
                if len(candidates) < 3:
                    for task in self._authority_bing_tasks(str(draft["markdown"]), asset["title_snapshot"], asset["keyword"]):
                        try:
                            results = self.server.competitor_content_client.search_bing(query=task["query"], locale=asset["locale"], max_results=8)
                        except GoogleSerpProtocolError as error:
                            reason = f"Bing fallback search failed: {error}"
                            audit_rows.append({"section_heading": task["section_heading"], "claim_topic": task["claim_topic"], "query": task["query"], "url": "", "title": task["section_heading"], "domain": "", "rank": "0", "status": "search_error", "reason": reason})
                            continue
                        add_results(task, results, provider="bing")
                        if len(candidates) >= max_authority_candidates:
                            break
                if not candidates:
                    with connection:
                        self._save_authority_search_audit(connection, search_run_id, project_id, asset_id, audit_rows)
                    raise ValueError("Configured search providers returned no usable HTML candidates from the authority-domain allowlist.")
                with connection:
                    self._save_authority_search_audit(connection, search_run_id, project_id, asset_id, audit_rows)
                    for candidate in candidates:
                        connection.execute("INSERT INTO authority_search_results(search_run_id,project_id,content_asset_id,section_heading,claim_topic,search_query,rank,title,url,domain,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (search_run_id, project_id, asset_id, candidate["section_heading"], candidate["claim_topic"], candidate["query"], int(candidate["rank"]), candidate["title"], candidate["url"], candidate["domain"], "pending"))
                batch = self.server.competitor_content_client.extract_many([item["url"] for item in candidates], max_workers=5)
                accepted: list[dict[str, Any]] = []
                for candidate in candidates:
                    fetched = batch.get(candidate["url"])
                    if not isinstance(fetched, Mapping):
                        reason = str(fetched) if isinstance(fetched, Exception) else "The search result could not be opened as a usable HTML content page."
                        skipped.append({"url": candidate["url"], "title": candidate["title"], "reason": reason})
                        with connection:
                            connection.execute("UPDATE authority_search_results SET status='skipped',error_summary=? WHERE search_run_id=? AND url=?", (reason, search_run_id, candidate["url"]))
                            self._remember_authority_exclusion(connection, project_id, candidate["url"], reason)
                        continue
                    relevant, relevance_reason = self._authority_source_relevance(
                        keyword=str(asset["keyword"]),
                        article_title=str(asset["title_snapshot"]),
                        section_heading=candidate["section_heading"],
                        source_title=str(fetched.get("title") or candidate["title"]),
                        source_content=str(fetched.get("content") or ""),
                    )
                    if not relevant:
                        reason = f"Skipped after opening: unrelated to this article section. {relevance_reason}"
                        skipped.append({"url": candidate["url"], "title": candidate["title"], "reason": reason})
                        with connection:
                            connection.execute("UPDATE authority_search_results SET status='skipped',error_summary=? WHERE search_run_id=? AND url=?", (reason, search_run_id, candidate["url"]))
                            self._remember_authority_exclusion(connection, project_id, candidate["url"], reason)
                        continue
                    existing = connection.execute("SELECT * FROM authority_source_library WHERE project_id=? AND url=?", (project_id, candidate["url"])).fetchone()
                    if existing is None:
                        source_type, authority_level = self._authority_source_profile(candidate["domain"])
                        classification = {"relevance": "accept", "reason": f"Verified {candidate['provider']} result matched the article claim and passed the authority-domain allowlist.", "summary": f"Verified {candidate['provider']} authority-search source for: {candidate['claim_topic']}", "tags": [item for item in re.findall(r"[A-Za-z0-9]{3,}", candidate["claim_topic"])][:8], "authority_level": authority_level, "supported_claim_topics": [candidate["claim_topic"]], "evidence_gaps": []}
                        with connection:
                            cursor = connection.execute("INSERT INTO authority_source_library(project_id,title,source_type,url,publisher,content,authority_level,tags_json,classification_json,summary) VALUES(?,?,?,?,?,?,?,?,?,?)", (project_id, str(fetched.get("title") or candidate["title"] or candidate["domain"]), source_type, candidate["url"], str(fetched.get("domain") or candidate["domain"]), str(fetched.get("content") or "")[:30000], authority_level, json.dumps(classification["tags"], ensure_ascii=False), json.dumps(classification, ensure_ascii=False), classification["summary"]))
                            existing = connection.execute("SELECT * FROM authority_source_library WHERE id=?", (cursor.lastrowid,)).fetchone()
                    with connection:
                        connection.execute("INSERT OR IGNORE INTO content_authority_source_links(project_id,content_asset_id,authority_source_id,section_heading,claim_topic) VALUES(?,?,?,?,?)", (project_id, asset_id, existing["id"], candidate["section_heading"], candidate["claim_topic"]))
                        connection.execute("UPDATE authority_search_results SET status='accepted',error_summary=NULL WHERE search_run_id=? AND url=?", (search_run_id, candidate["url"]))
                    accepted.append(self._authority_source_payload(existing))
                    if len(accepted) >= 5:
                        with connection:
                            connection.execute("UPDATE authority_search_results SET status='skipped',error_summary='Skipped: enough verified authority sources were already collected for this run.' WHERE search_run_id=? AND status='pending'", (search_run_id,))
                        break
                if accepted:
                    with connection:
                        connection.execute("UPDATE content_assets SET status='ready_to_publish',updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?", (asset_id, project_id))
        except (sqlite3.Error, ValueError, GoogleSerpProtocolError, SerperSearchProtocolError, CompetitorContentProtocolError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        providers = sorted({candidate["provider"] for candidate in candidates})
        self._json(HTTPStatus.CREATED, {"article": asset["title_snapshot"], "provider": "_with_".join(providers) + "_search", "model": None, "search_run_id": search_run_id, "candidates_checked": len(candidates) + len(audit_rows), "saved": accepted, "skipped": skipped})

    @staticmethod
    def _normalized_authority_url(url: str) -> str:
        return url.split("#", 1)[0].rstrip("/").casefold()

    @staticmethod
    def _authority_download_url(url: str) -> bool:
        path = urlparse(url).path.casefold()
        return path.endswith((".pdf", ".xls", ".xlsx", ".doc", ".docx", ".ppt", ".pptx", ".zip", ".csv"))

    @staticmethod
    def _authority_non_article_url(url: str) -> bool:
        parsed = urlparse(url)
        value = f"{parsed.path.casefold()}?{parsed.query.casefold()}"
        return any(marker in value for marker in AUTHORITY_NON_ARTICLE_PATH_MARKERS)

    @staticmethod
    def _previously_unusable_authority_urls(connection: sqlite3.Connection, project_id: int) -> set[str]:
        rows = connection.execute("SELECT normalized_url FROM authority_url_exclusions WHERE project_id=?", (project_id,)).fetchall()
        return {str(row["normalized_url"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows}

    @staticmethod
    def _remember_authority_exclusion(connection: sqlite3.Connection, project_id: int, url: str, reason: str) -> None:
        normalized = KeywordDiscoveryRequestHandler._normalized_authority_url(url)
        if not normalized:
            return
        connection.execute("INSERT INTO authority_url_exclusions(project_id,normalized_url,reason) VALUES(?,?,?) ON CONFLICT(project_id,normalized_url) DO UPDATE SET reason=excluded.reason,last_seen_at=CURRENT_TIMESTAMP", (project_id, normalized, reason[:500]))

    @staticmethod
    def _save_authority_search_audit(connection: sqlite3.Connection, search_run_id: str, project_id: int, asset_id: int, rows: list[Mapping[str, str]]) -> None:
        for row in rows:
            connection.execute("INSERT INTO authority_search_results(search_run_id,project_id,content_asset_id,section_heading,claim_topic,search_query,rank,title,url,domain,status,error_summary) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (search_run_id, project_id, asset_id, row["section_heading"], row["claim_topic"], row["query"], int(row["rank"]), row["title"], row["url"] or None, row["domain"] or None, row["status"], row["reason"]))
            if row["status"] == "skipped" and row["url"]:
                KeywordDiscoveryRequestHandler._remember_authority_exclusion(connection, project_id, row["url"], row["reason"])

    @staticmethod
    def _authority_domain_allowed(domain: str) -> bool:
        host = domain.casefold().removeprefix("www.").rstrip(".")
        return host.endswith(".gov") or host.endswith(".edu") or host in {"iso.org", "astm.org", "wikipedia.org"} or host.endswith(".iso.org") or host.endswith(".astm.org") or host.endswith(".wikipedia.org")

    @staticmethod
    def _authority_source_profile(domain: str) -> tuple[str, str]:
        host = domain.casefold().removeprefix("www.")
        if host.endswith(".gov"): return "government", "authoritative"
        if host.endswith(".iso.org") or host.endswith(".astm.org") or host in {"iso.org", "astm.org"}: return "standard", "authoritative"
        if host.endswith(".wikipedia.org") or host == "wikipedia.org": return "industry_research", "supporting"
        return "industry_research", "authoritative"

    @staticmethod
    def _authority_google_tasks(markdown: str, title: str, keyword: str) -> list[dict[str, str]]:
        headings = [" ".join(value.split()) for value in re.findall(r"(?m)^##\s+([^\n#]+)", markdown) if value.strip()]
        topics = headings[:4] or [title]
        scope = "(site:.gov OR site:.edu OR site:iso.org OR site:astm.org OR site:wikipedia.org)"
        return [{"section_heading": heading, "claim_topic": heading, "query": f"{KeywordDiscoveryRequestHandler._authority_core_terms(keyword, heading)} {scope} {AUTHORITY_SEARCH_FILE_EXCLUSIONS}"[:1_500]} for heading in topics]

    @staticmethod
    def _authority_bing_tasks(markdown: str, title: str, keyword: str) -> list[dict[str, str]]:
        headings = [" ".join(value.split()) for value in re.findall(r"(?m)^##\s+([^\n#]+)", markdown) if value.strip()]
        heading = (headings[:1] or [title])[0]
        core_terms = KeywordDiscoveryRequestHandler._authority_core_terms(keyword, heading)
        return [{"section_heading": heading, "claim_topic": heading, "query": f"{core_terms} site:{domain} {AUTHORITY_SEARCH_FILE_EXCLUSIONS}"[:1_500]} for domain in ("gov", "edu", "iso.org", "astm.org", "wikipedia.org")]

    @staticmethod
    def _authority_core_terms(keyword: str, heading: str) -> str:
        """Keep authority searches narrow: buyer terms plus the H2's factual entity."""
        stop_words = {"a", "an", "and", "are", "as", "at", "by", "complete", "does", "each", "explained", "for", "from", "good", "guide", "how", "in", "is", "it", "means", "of", "on", "or", "short", "that", "the", "this", "to", "versus", "vs", "what", "why", "with", "your"}
        terms: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            normalized = value.casefold()
            if len(normalized) < 2 or normalized in stop_words or normalized in seen or any(character.isdigit() for character in normalized):
                return
            seen.add(normalized)
            terms.append(value)

        # A heading often lists standard codes such as IP20/IP54/IP65. The
        # shared alphabetic prefix is the real entity; individual codes make
        # a web search drift toward PDFs, CVs, and unrelated catalog files.
        for value in re.findall(r"[A-Za-z0-9]+", keyword):
            add(value)
        for value in re.findall(r"[A-Za-z]+\d+", heading):
            prefix = re.match(r"[A-Za-z]+", value)
            if prefix:
                add(prefix.group(0))
        for value in re.findall(r"[A-Za-z0-9]+", heading):
            add(value)
            if len(terms) >= 9:
                break
        return " ".join(terms) or " ".join(keyword.split())[:180]

    @staticmethod
    def _authority_source_relevance(*, keyword: str, article_title: str, section_heading: str, source_title: str, source_content: str = "") -> tuple[bool, str]:
        """Require a source to be about the article section, not merely .gov/.edu.

        A whitelist proves who published a page; it does not prove that the
        page supports an LED/IP-rating claim.  Two distinct subject terms must
        occur in the source title before we spend a request, and the opened
        page must repeat enough of those terms to be treated as evidence.
        """
        stop_words = {
            "a", "an", "and", "are", "as", "at", "by", "complete", "does", "each", "explained", "for", "from", "good", "guide", "how", "in", "is", "it", "of", "on", "or", "the", "this", "to", "vs", "what", "why", "with", "your",
            "answer", "chapter", "difference", "explained", "guide", "matters", "short", "step", "steps", "tips", "versus", "water",
        }

        def terms(value: str) -> set[str]:
            return {
                item.casefold() for item in re.findall(r"[A-Za-z][A-Za-z0-9-]*", value)
                if len(item) >= 2 and item.casefold() not in stop_words
            }

        subject_terms = terms(f"{keyword} {article_title} {section_heading}")
        title_terms = terms(source_title)
        title_matches = subject_terms & title_terms
        if len(title_matches) < 2:
            return False, f"The result title only matches {len(title_matches)} subject term(s): {', '.join(sorted(title_matches)) or 'none'}."
        if not source_content:
            return True, f"The result title has {len(title_matches)} matching subject terms: {', '.join(sorted(title_matches))}."
        content_terms = terms(source_content[:30_000])
        content_matches = subject_terms & content_terms
        score = len(title_matches) * 2 + len(content_matches)
        if score < 5:
            return False, f"The opened page has insufficient topical evidence (title matches: {len(title_matches)}, body matches: {len(content_matches)})."
        return True, f"Title and body match the article subject (title: {len(title_matches)}, body: {len(content_matches)})."

    def _research_authority_sources_ai_legacy(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        asset_id = self._integer(payload, "asset_id") if "asset_id" in payload else None
        if project_id is None or asset_id is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id and a completed article asset_id are required."}); return
        try:
            with self._database() as connection:
                if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None: raise ValueError("project does not exist")
                generator, provider, model = self._content_generator(payload)
                if generator is None: raise ValueError("Selected content provider is not configured.")
                asset = self._content_asset(connection, project_id, asset_id)
                draft = connection.execute("SELECT markdown FROM content_drafts WHERE id=?", (asset["current_draft_id"],)).fetchone() if asset["current_draft_id"] else None
                if draft is None: raise ValueError("a completed article is required before researching authority sources")
                article_topic = asset["title_snapshot"]
                evidence_context = self._authority_evidence_context(str(draft["markdown"]))
                search_run_id = uuid.uuid4().hex
                # A new Gemini recommendation run replaces only this
                # article's old citation links.  Source-library records stay
                # as project memory, but must pass Gemini verification again
                # before they can be cited by this article.
                with connection:
                    connection.execute("DELETE FROM authority_search_results WHERE project_id=? AND content_asset_id=?", (project_id, asset_id))
                    connection.execute("DELETE FROM content_authority_source_links WHERE project_id=? AND content_asset_id=?", (project_id, asset_id))
                def request_plan(context: str) -> Any:
                    data = {"title": asset["title_snapshot"], "keyword": asset["keyword"], "evidence_context": context, "authority_domain_policy": "Only .gov, .edu, iso.org, astm.org, or wikipedia.org. Recommend only exact public HTML pages, never downloads, document viewers, forums, generic homepages, or merely topic-adjacent pages."}
                    return generator.run_stage(stage="authority_research_plan", data=data) if callable(getattr(generator, "run_stage", None)) else generator.generate(stage="authority_research_plan", **data)
                retry_contexts = (evidence_context, evidence_context[:1_800], evidence_context[:1_000])
                raw_plan: Any | None = None
                last_error: ContentGenerationProtocolError | None = None
                for context in retry_contexts:
                    try:
                        raw_plan = request_plan(context)
                        break
                    except ContentGenerationProtocolError as error:
                        # A 524 or connection failure is emitted by the
                        # configured upstream proxy, not a reason to silently
                        # switch models. Retry the exact same locked model with
                        # a smaller evidence dossier, then report the failure.
                        if "authority_research_plan upstream HTTP 524" not in str(error) and "authority_research_plan network request failed" not in str(error): raise
                        last_error = error
                if raw_plan is None:
                    raise ContentGenerationProtocolError(f"{last_error} (same-model authority-link planning failed after 3 attempts.)")
                plan = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
                if not isinstance(plan, Mapping) or not isinstance(plan.get("source_candidates"), list): raise ContentGenerationProtocolError("AI authority source plan returned invalid JSON.")
                candidates: list[dict[str, str]] = []
                audit_rows: list[dict[str, str]] = []
                skipped: list[dict[str, Any]] = []
                seen_urls: set[str] = set()
                for candidate in plan["source_candidates"]:
                    if not isinstance(candidate, Mapping): continue
                    url = str(candidate.get("url", "")).strip()
                    parsed = urlsplit(url)
                    normalized = url.split("#", 1)[0].rstrip("/").casefold()
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc or normalized in seen_urls: continue
                    seen_urls.add(normalized)
                    title = str(candidate.get("title", "")).strip()
                    domain = parsed.netloc.casefold().removeprefix("www.")
                    claim_topic = str(candidate.get("claim_topic", "")).strip()
                    section_heading = str(candidate.get("section_heading", "")).strip()
                    if not self._authority_domain_allowed(domain):
                        reason = "Rejected before opening: Gemini proposed a domain outside the authority whitelist."
                        skipped.append({"url": url, "title": title, "reason": reason})
                        audit_rows.append({"section_heading": section_heading, "claim_topic": claim_topic, "query": "Gemini authority recommendation", "url": url, "title": title, "domain": domain, "rank": "0", "status": "skipped", "reason": reason})
                        continue
                    if self._authority_download_url(url) or self._authority_non_article_url(url):
                        reason = "Rejected before opening: Gemini proposed a download, document viewer, or non-article URL."
                        skipped.append({"url": url, "title": title, "reason": reason})
                        audit_rows.append({"section_heading": section_heading, "claim_topic": claim_topic, "query": "Gemini authority recommendation", "url": url, "title": title, "domain": domain, "rank": "0", "status": "skipped", "reason": reason})
                        continue
                    candidates.append({"url": url, "title": title, "domain": domain, "claim_topic": claim_topic, "section_heading": section_heading, "preferred_source_type": str(candidate.get("preferred_source_type", "")).strip()})
                    if len(candidates) >= 8: break
                if not candidates: raise ContentGenerationProtocolError("AI authority source plan returned no valid public URLs.")
                with connection:
                    self._save_authority_search_audit(connection, search_run_id, project_id, asset_id, audit_rows)
                    for rank, candidate in enumerate(candidates, 1):
                        connection.execute("INSERT INTO authority_search_results(search_run_id,project_id,content_asset_id,section_heading,claim_topic,search_query,rank,title,url,domain,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (search_run_id, project_id, asset_id, candidate["section_heading"], candidate["claim_topic"], "Gemini authority recommendation", rank, candidate["title"], candidate["url"], candidate["domain"], "pending"))
                # References follow the same robots policy as competitor
                # learning. A blocked page may be saved as a URL, never read.
                batch = self.server.competitor_content_client.extract_many([item["url"] for item in candidates], max_workers=5, respect_robots=True)
                accepted: list[dict[str, Any]] = []
                for candidate in candidates:
                    url = candidate["url"]
                    fetched = batch.get(url)
                    if not isinstance(fetched, Mapping):
                        reason = str(fetched) if isinstance(fetched, Exception) else "The Gemini-proposed URL could not be opened as a usable HTML content page."
                        skipped.append({"url": url, "title": candidate["title"], "reason": reason})
                        with connection:
                            connection.execute("UPDATE authority_search_results SET status='skipped',error_summary=? WHERE search_run_id=? AND url=?", (reason, search_run_id, url))
                        continue
                    requested_type = candidate["preferred_source_type"]
                    source_type = requested_type if requested_type in {"first_party", "standard", "certification", "government", "industry_research"} else self._authority_source_type(str(fetched.get("domain", "")))
                    data = {"topic": article_topic, "keyword": asset["keyword"], "claim_topic": candidate["claim_topic"], "section_heading": candidate["section_heading"], "title": fetched.get("title", ""), "source_type": source_type, "url": url, "publisher": fetched.get("domain", ""), "content": str(fetched.get("content", ""))[:30000], "verification_rule": "Reject unless the fetched title and body materially support this exact article section and claim. A prestigious but generic, adjacent, or wrong-product page must be rejected."}
                    try:
                        raw = generator.run_stage(stage="source_classification", data=data) if callable(getattr(generator, "run_stage", None)) else generator.generate(stage="source_classification", **data)
                        classification = json.loads(raw) if isinstance(raw, str) else raw
                    except ContentGenerationProtocolError as error:
                        reason = f"Gemini verification failed: {error}"
                        skipped.append({"url": url, "title": str(fetched.get("title", "")), "reason": reason})
                        with connection:
                            connection.execute("UPDATE authority_search_results SET title=?,status='skipped',error_summary=? WHERE search_run_id=? AND url=?", (str(fetched.get("title", "")), reason, search_run_id, url))
                        continue
                    if not isinstance(classification, Mapping) or classification.get("relevance") != "accept" or classification.get("authority_level") == "needs_review":
                        reason = str(classification.get("reason", "Gemini rejected this page because it did not support the article claim with sufficient authority.")) if isinstance(classification, Mapping) else "Gemini returned an invalid verification decision."
                        skipped.append({"url": url, "title": str(fetched.get("title", "")), "reason": reason})
                        with connection:
                            connection.execute("UPDATE authority_search_results SET title=?,status='skipped',error_summary=? WHERE search_run_id=? AND url=?", (str(fetched.get("title", "")), reason, search_run_id, url))
                        continue
                    tags = classification.get("tags") if isinstance(classification.get("tags"), list) else []
                    with connection:
                        row = connection.execute("SELECT * FROM authority_source_library WHERE project_id=? AND url=?", (project_id, url)).fetchone()
                        if row is None:
                            connection.execute("INSERT INTO authority_source_library(project_id,title,source_type,url,publisher,content,authority_level,tags_json,classification_json,summary) VALUES(?,?,?,?,?,?,?,?,?,?)", (project_id, str(data["title"]), source_type, url, str(data["publisher"]), str(data["content"]), str(classification["authority_level"]), json.dumps([tag for tag in tags if isinstance(tag, str)], ensure_ascii=False), json.dumps(dict(classification), ensure_ascii=False), classification.get("summary") if isinstance(classification.get("summary"), str) else None))
                            row = connection.execute("SELECT * FROM authority_source_library WHERE id=last_insert_rowid()").fetchone()
                        connection.execute(
                            "INSERT OR IGNORE INTO content_authority_source_links(project_id,content_asset_id,authority_source_id,section_heading,claim_topic) VALUES(?,?,?,?,?)",
                            (project_id, asset_id, row["id"], candidate["section_heading"] or None, candidate["claim_topic"] or None),
                        )
                        connection.execute("UPDATE authority_search_results SET title=?,status='accepted',error_summary=NULL WHERE search_run_id=? AND url=?", (str(data["title"]), search_run_id, url))
                    accepted.append(self._authority_source_payload(row))
                    if len(accepted) >= 5: break
        except (sqlite3.Error, ValueError, ContentGenerationProtocolError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        if accepted:
            with self._database() as connection, connection:
                connection.execute("UPDATE content_assets SET status='ready_to_publish',updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?", (asset_id, project_id))
        self._json(HTTPStatus.CREATED, {"article": article_topic, "provider": provider, "model": model, "search_run_id": search_run_id, "candidates_checked": len(candidates) + len(audit_rows), "saved": accepted, "skipped": skipped})

    @staticmethod
    def _seo_image_filename(keyword: str, heading: str, position: int) -> str:
        def slug(value: str) -> str:
            return (re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:72].strip("-") or "article-section")
        return f"{slug(keyword)}-{slug(heading)}-{position:02d}.webp"

    def _designer_image_prompt(self, *, keyword: str, heading: str, section_text: str) -> str:
        """Turn an article H2 into a compact marketing-art direction."""
        configuration = _provider_configuration(self.server.ai_settings_path, "openai")
        fallback = f"Modern minimalist marketing graphic for {heading}, illustrating {keyword} with one accurate real-world product or installation detail, European editorial design, light neutral background, soft gradients, refined technical materials, one coherent horizontal composition, no people, no text, no letters, no numbers, no IP codes, no labels, no cards, no tables, no charts, no logos, no watermark, no collage, and no unrelated product parts."
        if configuration is None:
            return fallback
        api_key, base_url, model = configuration
        system_prompt = """You are a graphic design expert who converts one SEO article section into one final text-to-image prompt for a polished professional marketing graphic. Read the article context and identify only the H2's central message. Make the visual directly support that one message, not the whole article. Use a light-colored minimalist European editorial aesthetic appropriate for a professional LinkedIn feed and blog: refined layout, soft gradients, subtle abstract shape overlays, accurate product materials, and one coherent visual concept. Never write or repeat article prose. Text is forbidden in the image: do not request words, letters, numbers, IP codes, badges, labels, cards, screens, charts, tables, captions, title text, logos, or watermarks. Avoid people unless the H2 is explicitly about installation action. Do not request surreal scenes, collages, repeated product lineups, or cropped objects. Output only the final English image prompt, no quotation marks, no explanation, 390 to 420 characters."""
        payload = {"model": model, "temperature": 0.35, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": json.dumps({"keyword": keyword, "h2": heading, "section_context": section_text[:1_400]}, ensure_ascii=False)}]}
        try:
            request = Request(f"{base_url.rstrip('/')}/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=90) as response:  # nosec B310 - configured prompt endpoint
                body = json.loads(response.read().decode("utf-8"))
            prompt = str(body["choices"][0]["message"]["content"] or "").strip().strip('"')
            prompt = " ".join(prompt.replace("```", "").split())
            if len(prompt) >= 260:
                guard = " No visible text, letters, numbers, IP codes, labels, cards, charts, typography, logos, or watermark."
                limit = 420 - len(guard)
                return prompt[:limit].rsplit(" ", 1)[0] + guard
        except Exception:
            pass
        return fallback

    def _ensure_section_image_prompts(self, asset_id: int, project_id: int) -> list[sqlite3.Row]:
        with self._database() as connection:
            asset = self._content_asset(connection, project_id, asset_id)
            draft = connection.execute("SELECT * FROM content_drafts WHERE id=?", (asset["current_draft_id"],)).fetchone() if asset["current_draft_id"] else None
            if draft is None: raise ValueError("a completed article is required before creating H2 image prompts")
            raw_sections = re.split(r"(?m)^##\s+", str(draft["markdown"]))[1:]
            sections = [(" ".join(chunk.partition("\n")[0].split()), " ".join(chunk.partition("\n")[2].split())) for chunk in raw_sections]
            # Some model outputs repeat the article title as the first H2.
            # That is an article heading, not a content section, so it must
            # never receive a hero image above the real body sections.
            canonical_title = re.sub(r"[^a-z0-9]+", " ", str(draft["title"] or "").casefold()).strip()
            indexed_sections = [
                (position, heading, body)
                for position, (heading, body) in enumerate(sections, 1)
                if heading and re.sub(r"[^a-z0-9]+", " ", heading.casefold()).strip() != canonical_title
            ]
            if not indexed_sections: raise ValueError("the article has no H2 sections to illustrate")
            prepared: list[tuple[int, str, str, str, str]] = []
            for position, heading, section_text in indexed_sections:
                prompt = self._designer_image_prompt(keyword=str(asset["keyword"]), heading=heading, section_text=section_text)
                alt_text = f"{asset['keyword']} — {heading}"
                filename = self._seo_image_filename(str(asset["keyword"]), heading, position)
                prepared.append((position, heading, prompt, alt_text, filename))
        with self._database() as connection, connection:
            # Creating prompts is also called by the "generate all" action.
            # Do not replace the rows wholesale here: doing so turns already
            # generated images back into pending work and needlessly spends
            # another image request every time the user retries failed H2s.
            existing_rows = connection.execute(
                "SELECT * FROM content_section_images WHERE project_id=? AND content_asset_id=? AND draft_id=?",
                (project_id, asset_id, draft["id"]),
            ).fetchall()
            existing_by_key = {(int(row["position"]), str(row["section_heading"])): row for row in existing_rows}
            prepared_keys: set[tuple[int, str]] = set()
            for position, heading, prompt, alt_text, filename in prepared:
                key = (position, heading)
                prepared_keys.add(key)
                current = existing_by_key.get(key)
                if current is None:
                    connection.execute(
                        "INSERT INTO content_section_images(project_id,content_asset_id,draft_id,section_heading,position,prompt,alt_text,seo_filename,provider,model) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (project_id, asset_id, draft["id"], heading, position, prompt, alt_text, filename, draft["provider"], _image_generation_model(self.server.ai_settings_path)),
                    )
                elif str(current["status"]) != "ready":
                    # A pending or failed item gets the current prompt and
                    # SEO metadata before it is retried. Ready images stay
                    # intact so a retry only targets the unsuccessful H2s.
                    connection.execute(
                        "UPDATE content_section_images SET prompt=?,alt_text=?,seo_filename=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (prompt, alt_text, filename, current["id"]),
                    )
            stale_ids = [int(row["id"]) for key, row in existing_by_key.items() if key not in prepared_keys]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                connection.execute(f"DELETE FROM content_section_images WHERE id IN ({placeholders})", stale_ids)
            return connection.execute("SELECT * FROM content_section_images WHERE draft_id=? ORDER BY position", (draft["id"],)).fetchall()

    def _create_section_image_prompts(self, asset_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None: return
        try: rows = self._ensure_section_image_prompts(asset_id, project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.CREATED, {"images": [dict(row) for row in rows]})

    @staticmethod
    def _generate_image_with_local_proxy(*, prompt: str, output_path: Path, model: str) -> bool:
        """Use the user-provided Windows image relay when it is available."""
        relay = Path(r"D:\网站\generate-image.ps1")
        if not relay.is_file():
            return False
        response_path = output_path.with_suffix(".response.json")
        last_error = ""
        for attempt in range(1, 4):
            try:
                completed = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(relay), "-Prompt", prompt, "-OutputPath", str(output_path), "-Size", CONTENT_SECTION_IMAGE_SIZE, "-N", "1", "-Model", model],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=240,
                    check=False,
                )
                if completed.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0:
                    response_path.unlink(missing_ok=True)
                    return True
                last_error = (completed.stderr or completed.stdout or f"exit code {completed.returncode}").strip()[:500]
            except (OSError, subprocess.TimeoutExpired) as error:
                last_error = str(error)[:500]
            if attempt < 3:
                time.sleep(attempt * 2)
        raise ValueError(f"local image relay failed after 3 attempts: {last_error}")

    @staticmethod
    def _normalize_section_image_to_webp(source_path: Path, output_path: Path) -> None:
        """Create the fixed 800×600 WebP used by article sections.

        Providers may return PNG/JPEG bytes even when a .webp output path was
        requested. Re-encoding locally makes browser delivery and WordPress
        uploads consistent without trusting the provider's file extension.
        """
        temporary = output_path.with_suffix(".conversion.webp")
        try:
            with Image.open(source_path) as raw:
                image = ImageOps.exif_transpose(raw).convert("RGB")
                rendered = ImageOps.fit(image, CONTENT_SECTION_IMAGE_DIMENSIONS, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
                rendered.save(temporary, format="WEBP", quality=84, method=6)
            temporary.replace(output_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _generate_section_image_row(self, image_id: int, project_id: int) -> dict[str, Any]:
        with self._database() as connection:
            image = connection.execute("SELECT * FROM content_section_images WHERE id=? AND project_id=?", (image_id, project_id)).fetchone()
            if image is None: raise ValueError("image prompt does not exist in this website")
        existing_filename = str(image["seo_filename"] or f"section-{image_id}.webp")
        filename = Path(existing_filename).with_suffix(".webp").name
        directory = WEB_ROOT / "generated-images" / str(project_id); directory.mkdir(parents=True, exist_ok=True)
        output_path = directory / filename
        legacy_path = directory / existing_filename
        image_configuration = _image_generation_configuration(self.server.ai_settings_path)
        if image_configuration is None: raise ValueError("configure the selected image provider before generating images")
        image_provider, api_key, base_url, image_model = image_configuration
        # A provider can finish writing the PNG immediately before the local
        # process or the server is interrupted. Recover that completed file on
        # the next retry instead of generating and charging for it again.
        if not output_path.is_file() and legacy_path.is_file() and legacy_path.stat().st_size > 0:
            self._normalize_section_image_to_webp(legacy_path, output_path)
        if output_path.is_file() and output_path.stat().st_size > 0:
            self._normalize_section_image_to_webp(output_path, output_path)
            output_path.with_suffix(".response.json").unlink(missing_ok=True)
            local_url = f"/generated-images/{project_id}/{filename}"
            with self._database() as connection, connection:
                connection.execute(
                    "UPDATE content_section_images SET status='ready',image_url=?,seo_filename=?,provider=?,model=?,error_summary=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (local_url, filename, image_provider, image_model, image_id),
                )
                recovered = connection.execute("SELECT * FROM content_section_images WHERE id=?", (image_id,)).fetchone()
            return dict(recovered)
        with self._database() as connection, connection:
            connection.execute("UPDATE content_section_images SET status='generating',error_summary=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (image_id,))
        generated_with_relay = image_provider == "openai" and self._generate_image_with_local_proxy(prompt=str(image["prompt"]), output_path=output_path, model=image_model)
        if not generated_with_relay:
            request_payload: dict[str, Any] = {"model": image_model, "prompt": image["prompt"]}
            if image_provider == "siliconflow":
                request_payload.update({"image_size": CONTENT_SECTION_IMAGE_SIZE, "batch_size": 1, "num_inference_steps": 20, "guidance_scale": 7.5})
            else:
                request_payload["size"] = CONTENT_SECTION_IMAGE_SIZE
            request = Request(f"{base_url.rstrip('/')}/images/generations", data=json.dumps(request_payload).encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
            # OpenAI-compatible proxy connections can occasionally close during a
            # long image render. Retry the same configured model and prompt before
            # marking a section failed; no model fallback is used.
            result: Any = None
            last_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    with urlopen(request, timeout=180) as response:  # nosec B310 - configured OpenAI-compatible image endpoint
                        result = json.loads(response.read().decode("utf-8"))
                    break
                except (OSError, TimeoutError, ValueError) as error:
                    last_error = error
                    if attempt < 3:
                        time.sleep(_image_retry_delay(error, attempt, image_provider))
            if result is None:
                raise ValueError(f"image generation failed after 3 attempts: {last_error}")
            data = (result.get("data") or result.get("images")) if isinstance(result, Mapping) else None
            item = data[0] if isinstance(data, list) and data and isinstance(data[0], Mapping) else None
            if not isinstance(item, Mapping): raise ValueError("image provider returned no image data")
            b64_json, remote_url = item.get("b64_json"), item.get("url")
            if isinstance(b64_json, str) and b64_json:
                output_path.write_bytes(base64.b64decode(b64_json))
            elif isinstance(remote_url, str) and remote_url.startswith(("http://", "https://")):
                with urlopen(Request(remote_url, headers={"User-Agent": "SEOContentImageStore/1.0"}), timeout=60) as download:  # nosec B310 - configured image result
                    downloaded_bytes = download.read(12_000_000)
                    content_type = download.headers.get_content_type()
                    if not content_type.startswith("image/") and not _looks_like_image_bytes(downloaded_bytes):
                        raise ValueError(f"image provider URL did not return an image (Content-Type: {content_type})")
                    output_path.write_bytes(downloaded_bytes)
            else: raise ValueError("image provider returned neither b64_json nor a downloadable image URL")
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise ValueError("image provider did not create a usable local image file")
        self._normalize_section_image_to_webp(output_path, output_path)
        local_url = f"/generated-images/{project_id}/{filename}"
        with self._database() as connection, connection:
            connection.execute("UPDATE content_section_images SET status='ready',image_url=?,seo_filename=?,provider=?,model=?,error_summary=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (local_url, filename, image_provider, image_model, image_id))
            row = connection.execute("SELECT * FROM content_section_images WHERE id=?", (image_id,)).fetchone()
        return dict(row)

    def _generate_section_image(self, image_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None: return
        try: image = self._generate_section_image_row(image_id, project_id)
        except Exception as error:
            with self._database() as connection, connection: connection.execute("UPDATE content_section_images SET status='failed',error_summary=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?", (str(error)[:500], image_id, project_id))
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error)}); return
        self._json(HTTPStatus.CREATED, image)

    def _generate_all_section_images(self, asset_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None: return
        try:
            rows = self._ensure_section_image_prompts(asset_id, project_id)
            image_directory = WEB_ROOT / "generated-images" / str(project_id)
            # A frontend build used to empty web/, which can leave a ready
            # database row pointing at a missing PNG. Treat that as unfinished
            # so one-click generation repairs the physical file as well.
            image_ids = [
                int(row["id"])
                for row in rows
                if str(row["status"]) != "ready"
                or not (image_directory / Path(str(row["seo_filename"] or f"section-{row['id']}.webp")).with_suffix(".webp").name).is_file()
            ]
            completed: list[dict[str, Any]] = []; failures: list[dict[str, str]] = []
            # The HTTP request already runs in a server worker thread, so a
            # second executor adds no benefit. A plain ordered loop keeps one
            # image request in flight, makes status transitions deterministic,
            # and lets a retry continue from the first unfinished H2.
            uses_siliconflow = _image_generation_provider(self.server.ai_settings_path) == "siliconflow"
            for index, image_id in enumerate(image_ids):
                # The free SiliconFlow image endpoint throttles bursts. Space
                # bulk requests so a long H2 article does not fail halfway
                # through simply because the first few images were accepted.
                if uses_siliconflow and index:
                    time.sleep(SILICONFLOW_IMAGE_REQUEST_INTERVAL_SECONDS)
                try:
                    completed.append(self._generate_section_image_row(image_id, project_id))
                except Exception as error:
                    with self._database() as connection, connection:
                        connection.execute(
                            "UPDATE content_section_images SET status='failed',error_summary=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?",
                            (str(error)[:500], image_id, project_id),
                        )
                    failures.append({"id": str(image_id), "error": str(error)})
            completed.sort(key=lambda row: int(row["position"]))
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.CREATED, {"generated": completed, "failed": failures})

    def _open_gsc_browser(self, project_id: int) -> None:
        with self._database() as connection:
            project = connection.execute("SELECT site_url,name FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "project does not exist"}); return
        try:
            property_url = str(project["site_url"] or project["name"] or "")
            self._json(HTTPStatus.OK, self.server.gsc_browser_client.open_console(property_url=property_url))
        except GscBrowserCaptureError as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})

    def _capture_gsc_browser_rows(self, project_id: int) -> None:
        try:
            with self._database() as connection:
                project = connection.execute("SELECT site_url,name FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None: raise ValueError("project does not exist")
            fallback_page = str(project["site_url"] or project["name"] or "")
            if not fallback_page.startswith(("http://", "https://")): raise ValueError("save this project's website URL before capturing GSC rows")
            rows = self.server.gsc_browser_client.capture_visible_rows(fallback_page_url=fallback_page)
            with self._database() as connection, connection:
                for row in rows:
                    connection.execute("INSERT INTO project_gsc_query_rows(project_id,property_url,query,page_url,clicks,impressions,ctr,position,collected_at) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(project_id,property_url,query,page_url) DO UPDATE SET clicks=excluded.clicks,impressions=excluded.impressions,ctr=excluded.ctr,position=excluded.position,collected_at=CURRENT_TIMESTAMP", (project_id, fallback_page, str(row["query"]), str(row["page_url"]), float(row["clicks"]), float(row["impressions"]), float(row["ctr"]), float(row["position"])))
        except (GscBrowserCaptureError, ValueError, sqlite3.Error) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"GSC browser capture failed: {str(error)[:300]}"}); return
        self._list_gsc_anchor_candidates(project_id, synced=len(rows))

    def _capture_gsc_ranked_pages(self, project_id: int) -> None:
        """Replace this project's GSC mapping with a fresh signed-in UI capture."""
        try:
            with self._database() as connection:
                project = connection.execute("SELECT site_url,name FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None: raise ValueError("project does not exist")
            fallback_page = str(project["site_url"] or project["name"] or "")
            if not fallback_page.startswith(("http://", "https://")): raise ValueError("save this project's website URL before capturing GSC rows")
            # The action is a refresh, not an append. Clear only this
            # project's previously collected GSC query-to-page mappings; it
            # never touches its keyword library, content, or other projects.
            with self._database() as connection, connection:
                cleared = connection.execute("DELETE FROM project_gsc_query_rows WHERE project_id=?", (project_id,)).rowcount
            result = self.server.gsc_browser_client.capture_ranked_query_pages(fallback_page_url=fallback_page)
            rows = result["rows"]
            with self._database() as connection, connection:
                for row in rows:
                    connection.execute("INSERT INTO project_gsc_query_rows(project_id,property_url,query,page_url,clicks,impressions,ctr,position,collected_at) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(project_id,property_url,query,page_url) DO UPDATE SET clicks=excluded.clicks,impressions=excluded.impressions,ctr=excluded.ctr,position=excluded.position,collected_at=CURRENT_TIMESTAMP", (project_id, fallback_page, str(row["query"]), str(row["page_url"]), float(row["clicks"]), float(row["impressions"]), float(row["ctr"]), float(row["position"])))
        except (GscBrowserCaptureError, ValueError, sqlite3.Error) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"GSC ranked-page capture failed: {str(error)[:360]}"}); return
        self._list_gsc_anchor_candidates(project_id, synced=len(rows), capture={**result, "cleared": cleared})

    def _get_gsc_settings(self) -> None:
        settings = _gsc_settings(self.server.ai_settings_path)
        with self._credential_database() as connection:
            connection = connection.execute("SELECT account_email FROM gsc_oauth_credentials WHERE id=1").fetchone()
        self._json(HTTPStatus.OK, {"configured": bool(settings["client_id"] and settings["client_secret"]), "client_id": settings["client_id"], "client_secret_configured": bool(settings["client_secret"]), "connected": connection is not None, "account_email": str(connection["account_email"]) if connection else "", "redirect_uri": self._gsc_redirect_uri()})

    def _save_gsc_settings(self, payload: Mapping[str, Any]) -> None:
        client_id = self._text(payload, "client_id")
        client_secret = self._optional_text(payload, "client_secret")
        if client_id is None:
            return
        if not client_id.endswith(".apps.googleusercontent.com"):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Google OAuth client ID must end with .apps.googleusercontent.com."}); return
        document = dict(_ai_settings_document(self.server.ai_settings_path)); integrations = dict(document.get("integrations") or {}); current = dict(integrations.get("gsc") or {})
        current["client_id"] = client_id
        if client_secret: current["client_secret"] = client_secret
        integrations["gsc"] = current; document["integrations"] = integrations
        self.server.ai_settings_path.parent.mkdir(parents=True, exist_ok=True); self.server.ai_settings_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        self._get_gsc_settings()

    def _gsc_redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/api/gsc/oauth/callback"

    def _start_gsc_oauth(self, project_id: int) -> None:
        settings = _gsc_settings(self.server.ai_settings_path)
        if not settings["client_id"] or not settings["client_secret"]:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Save the Google OAuth client ID and client secret first."}); return
        with self._database() as connection:
            if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "project does not exist"}); return
        state = uuid.uuid4().hex; self.server.gsc_oauth_states[state] = project_id
        query = urlencode({"client_id": settings["client_id"], "redirect_uri": self._gsc_redirect_uri(), "response_type": "code", "access_type": "offline", "prompt": "consent", "scope": "https://www.googleapis.com/auth/webmasters.readonly https://www.googleapis.com/auth/userinfo.email", "state": state})
        self.send_response(HTTPStatus.FOUND); self.send_header("Location", f"https://accounts.google.com/o/oauth2/v2/auth?{query}"); self.end_headers()

    def _gsc_oauth_callback(self, query: Mapping[str, list[str]]) -> None:
        state, code = (query.get("state") or [""])[0], (query.get("code") or [""])[0]
        project_id = self.server.gsc_oauth_states.pop(state, None)
        error = (query.get("error") or [""])[0]
        if project_id is None or error or not code:
            self._serve_gsc_callback_page(project_id, f"Google authorization failed: {error or 'missing authorization code'}"); return
        settings = _gsc_settings(self.server.ai_settings_path)
        try:
            response = requests.post("https://oauth2.googleapis.com/token", data={"code": code, "client_id": settings["client_id"], "client_secret": settings["client_secret"], "redirect_uri": self._gsc_redirect_uri(), "grant_type": "authorization_code"}, timeout=25)
            response.raise_for_status(); token = response.json(); refresh_token = str(token.get("refresh_token") or "")
            if not refresh_token: raise ValueError("Google did not return a refresh token; revoke this app in Google Account permissions and connect again")
            email = ""
            profile = requests.get("https://www.googleapis.com/oauth2/v2/userinfo", headers={"Authorization": f"Bearer {token.get('access_token', '')}"}, timeout=20)
            if profile.ok: email = str(profile.json().get("email") or "")
            with self._credential_database() as connection, connection:
                connection.execute("INSERT INTO gsc_oauth_credentials(id,account_email,refresh_token,scopes) VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET account_email=excluded.account_email,refresh_token=excluded.refresh_token,scopes=excluded.scopes,updated_at=CURRENT_TIMESTAMP", (email, self._protect_gsc_token(refresh_token), "webmasters.readonly userinfo.email"))
        except Exception as exception:
            self._serve_gsc_callback_page(project_id, f"Google token exchange failed: {str(exception)[:180]}"); return
        self._serve_gsc_callback_page(project_id, "Google Search Console connected. You can now select a property and sync rankings.", success=True)

    def _serve_gsc_callback_page(self, project_id: int | None, message: str, success: bool = False) -> None:
        destination = f"/gsc?project_id={project_id or ''}&gsc={'connected' if success else 'failed'}"
        safe_message = html.escape(message)
        body = f"<!doctype html><meta charset='utf-8'><title>Google Search Console</title><body style='font-family:Segoe UI,Arial;padding:48px;color:#14242d'><h1>{'已绑定 Google Search Console' if success else 'Google Search Console 绑定失败'}</h1><p>{safe_message}</p><p><a href='{destination}'>返回 SEO 中控系统</a></p><script>setTimeout(()=>location.href={json.dumps(destination)},1200)</script></body>"
        raw = body.encode("utf-8"); self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def _get_gsc_project(self, project_id: int) -> None:
        with self._database() as connection:
            project = connection.execute("SELECT id,name,site_url FROM projects WHERE id=?", (project_id,)).fetchone(); selected = connection.execute("SELECT property_url FROM project_gsc_properties WHERE project_id=?", (project_id,)).fetchone()
        with self._credential_database() as credential_connection:
            account = credential_connection.execute("SELECT account_email FROM gsc_oauth_credentials WHERE id=1").fetchone()
        if project is None: self._json(HTTPStatus.NOT_FOUND, {"error": "project does not exist"}); return
        properties: list[str] = []; warning = ""
        if account:
            try: properties = self._gsc_properties()
            except Exception as error: warning = str(error)[:220]
        self._json(HTTPStatus.OK, {"configured": bool(_gsc_settings(self.server.ai_settings_path)["client_id"]), "connected": account is not None, "account_email": str(account["account_email"]) if account else "", "project_site_url": str(project["site_url"] or ""), "property_url": str(selected["property_url"]) if selected else "", "properties": properties, "warning": warning, "redirect_uri": self._gsc_redirect_uri()})

    def _save_gsc_property(self, project_id: int, payload: Mapping[str, Any]) -> None:
        property_url = self._text(payload, "property_url")
        if property_url is None: return
        try:
            if property_url not in self._gsc_properties(): raise ValueError("selected Search Console property is not accessible to the connected Google account")
            with self._database() as connection, connection: connection.execute("INSERT INTO project_gsc_properties(project_id,property_url) VALUES(?,?) ON CONFLICT(project_id) DO UPDATE SET property_url=excluded.property_url,updated_at=CURRENT_TIMESTAMP", (project_id, property_url))
        except Exception as error: self._json(HTTPStatus.BAD_GATEWAY, {"error": f"GSC property binding failed: {str(error)[:260]}"}); return
        self._get_gsc_project(project_id)

    def _sync_gsc_rankings(self, project_id: int, payload: Mapping[str, Any]) -> None:
        days = max(7, min(365, self._integer(payload, "days") or 90))
        try:
            with self._database() as connection: selected = connection.execute("SELECT property_url FROM project_gsc_properties WHERE project_id=?", (project_id,)).fetchone()
            if selected is None: raise ValueError("select a Search Console property for this website first")
            property_url = str(selected["property_url"]); end = date.today() - timedelta(days=3); start = end - timedelta(days=days)
            rows = self._gsc_request("POST", f"https://searchconsole.googleapis.com/webmasters/v3/sites/{quote(property_url, safe='')}/searchAnalytics/query", {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["query", "page"], "rowLimit": 500}) .get("rows", [])
            with self._database() as connection, connection:
                for row in rows:
                    keys = row.get("keys") or []
                    if len(keys) != 2: continue
                    connection.execute("INSERT INTO project_gsc_query_rows(project_id,property_url,query,page_url,clicks,impressions,ctr,position,collected_at) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(project_id,property_url,query,page_url) DO UPDATE SET clicks=excluded.clicks,impressions=excluded.impressions,ctr=excluded.ctr,position=excluded.position,collected_at=CURRENT_TIMESTAMP", (project_id, property_url, str(keys[0]), str(keys[1]), float(row.get("clicks") or 0), float(row.get("impressions") or 0), float(row.get("ctr") or 0), float(row.get("position") or 0)))
        except Exception as error: self._json(HTTPStatus.BAD_GATEWAY, {"error": f"GSC ranking sync failed: {str(error)[:300]}"}); return
        self._list_gsc_anchor_candidates(project_id, days=days, synced=len(rows))

    def _list_gsc_anchor_candidates(self, project_id: int, days: int | None = None, synced: int | None = None, capture: Mapping[str, Any] | None = None) -> None:
        with self._database() as connection:
            rows = connection.execute("SELECT query,page_url,clicks,impressions,ctr,position,collected_at FROM project_gsc_query_rows WHERE project_id=? ORDER BY clicks DESC,impressions DESC,position ASC LIMIT 80", (project_id,)).fetchall()
        self._json(HTTPStatus.OK, {"anchors": [dict(row) for row in rows], "days": days, "synced": synced, "capture": dict(capture) if capture else None})

    def _learn_from_published_gsc_content(self, project_id: int, payload: Mapping[str, Any]) -> None:
        """Queue the durable, evidence-gated GSC feedback workflow."""
        self._queue_gsc_feedback_learning(project_id, payload)

    def _list_content_gsc_performance(self, project_id: int) -> None:
        try:
            with self._database() as connection:
                self._project_exists(connection, project_id)
                rows = connection.execute(
                    """SELECT snapshots.*,assets.title_snapshot,keywords.keyword
                       FROM content_gsc_performance_snapshots snapshots
                       JOIN content_assets assets ON assets.id=snapshots.content_asset_id
                       JOIN keywords ON keywords.id=assets.keyword_id
                       WHERE snapshots.project_id=?
                       ORDER BY snapshots.collected_at DESC,snapshots.id DESC LIMIT 100""",
                    (project_id,),
                ).fetchall()
                values: list[dict[str, Any]] = []
                for row in rows:
                    value = dict(row)
                    query_rows = connection.execute("SELECT query,page_url,clicks,impressions,ctr,position FROM content_gsc_performance_rows WHERE snapshot_id=? ORDER BY impressions DESC,clicks DESC,position ASC LIMIT 8", (row["id"],)).fetchall()
                    value["top_queries"] = [dict(query_row) for query_row in query_rows]
                    values.append(value)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.OK, values)

    @staticmethod
    def _content_quality_score(
        draft: sqlite3.Row, *, authority_source_count: int, outline_section_count: int
    ) -> tuple[int, list[dict[str, Any]]]:
        """Return a reproducible, evidence-first quality score out of 100.

        This is deliberately a checklist-derived score rather than an AI claim
        that an article is good.  It becomes useful next to real GSC data, but
        it never treats a temporary ranking movement as proof of causation.
        """
        qa_status = str(draft["qa_status"] or "not_run")
        qa_points = {"approved": 30, "needs_verification": 16, "needs_revision": 7, "not_run": 10}.get(qa_status, 6)
        unresolved = json.loads(str(draft["unresolved_verify_json"] or "[]"))
        unresolved_count = len(unresolved) if isinstance(unresolved, list) else 0
        verification_points = max(0, 20 - min(unresolved_count, 5) * 4)
        authority_points = min(max(authority_source_count, 0), 4) * 5
        heading_points = 15 if 3 <= outline_section_count <= 9 else (9 if outline_section_count else 0)
        word_count = len(re.findall(r"\b[\w'-]+\b", str(draft["markdown"] or "")))
        depth_points = 15 if word_count >= 1200 else (11 if word_count >= 800 else (7 if word_count >= 500 else 3))
        score = max(0, min(100, qa_points + verification_points + authority_points + heading_points + depth_points))
        breakdown = [
            {"key": "qa", "label": "QA 状态", "points": qa_points, "max": 30, "note": qa_status},
            {"key": "verification", "label": "待验证事实", "points": verification_points, "max": 20, "note": f"{unresolved_count} 条待验证"},
            {"key": "authority", "label": "权威来源", "points": authority_points, "max": 20, "note": f"{authority_source_count} 个已关联来源"},
            {"key": "structure", "label": "H2 结构", "points": heading_points, "max": 15, "note": f"{outline_section_count} 个 H2"},
            {"key": "depth", "label": "正文深度", "points": depth_points, "max": 15, "note": f"约 {word_count} 词"},
        ]
        return score, breakdown

    @staticmethod
    def _content_performance_score(snapshot: sqlite3.Row, previous: sqlite3.Row | None) -> tuple[int, list[dict[str, Any]]]:
        """Score observed search performance only when its evidence is qualified."""
        position = float(snapshot["average_position"] or 0)
        ctr = float(snapshot["ctr"] or 0)
        impressions = float(snapshot["impressions"] or 0)
        queries = int(snapshot["query_count"] or 0)
        position_points = 15 if 0 < position <= 10 else (12 if position <= 20 else (8 if position <= 30 else (4 if position <= 40 else 1)))
        ctr_points = min(10, round(ctr * 200))
        impression_points = min(8, round(impressions / 125))
        query_points = min(7, queries)
        trend_note = "尚无可比前次快照"
        trend_points = 0
        if previous is not None:
            previous_impressions = float(previous["impressions"] or 0)
            previous_position = float(previous["average_position"] or 0)
            impression_delta = impressions - previous_impressions
            position_delta = previous_position - position if previous_position and position else 0.0
            trend_points = max(0, min(5, (3 if impression_delta > 0 else 0) + (2 if position_delta > 0 else 0)))
            trend_note = f"展现 {impression_delta:+.0f}；排名变化 {position_delta:+.1f}（正值为改善）"
        score = max(0, min(45, position_points + ctr_points + impression_points + query_points + trend_points))
        return score, [
            {"key": "position", "label": "平均排名", "points": position_points, "max": 15, "note": f"{position:.1f}" if position else "暂无排名"},
            {"key": "ctr", "label": "点击率", "points": ctr_points, "max": 10, "note": f"{ctr * 100:.1f}%"},
            {"key": "impressions", "label": "展现样本", "points": impression_points, "max": 8, "note": f"{impressions:.0f}"},
            {"key": "queries", "label": "覆盖查询", "points": query_points, "max": 7, "note": f"{queries} 个"},
            {"key": "trend", "label": "相邻快照趋势", "points": trend_points, "max": 5, "note": trend_note},
        ]

    @staticmethod
    def _spearman_correlation(values: list[tuple[float, float]]) -> float | None:
        if len(values) < 5:
            return None

        def ranks(items: list[float]) -> list[float]:
            indexed = sorted(enumerate(items), key=lambda item: item[1])
            result = [0.0] * len(items)
            index = 0
            while index < len(indexed):
                end = index + 1
                while end < len(indexed) and indexed[end][1] == indexed[index][1]:
                    end += 1
                average_rank = (index + 1 + end) / 2
                for item_index, _value in indexed[index:end]:
                    result[item_index] = average_rank
                index = end
            return result

        quality_ranks, performance_ranks = ranks([item[0] for item in values]), ranks([item[1] for item in values])
        mean_quality, mean_performance = sum(quality_ranks) / len(values), sum(performance_ranks) / len(values)
        numerator = sum((quality - mean_quality) * (performance - mean_performance) for quality, performance in zip(quality_ranks, performance_ranks))
        denominator = math.sqrt(sum((quality - mean_quality) ** 2 for quality in quality_ranks) * sum((performance - mean_performance) ** 2 for performance in performance_ranks))
        return round(numerator / denominator, 3) if denominator else None

    def _list_content_effectiveness(self, project_id: int) -> None:
        """Expose quality/GSC association without claiming a causal result."""
        try:
            with self._database() as connection:
                self._project_exists(connection, project_id)
                published = connection.execute(
                    """SELECT publications.content_asset_id,publications.wordpress_url,publications.draft_id AS published_draft_id,
                              publications.created_at AS published_at,assets.title_snapshot
                       FROM content_wordpress_publications publications
                       JOIN content_assets assets ON assets.id=publications.content_asset_id
                       WHERE publications.project_id=? AND publications.status='publish' AND assets.deleted_at IS NULL
                       ORDER BY publications.id DESC""",
                    (project_id,),
                ).fetchall()
                latest_publication_by_asset: dict[int, sqlite3.Row] = {}
                for publication in published:
                    latest_publication_by_asset.setdefault(int(publication["content_asset_id"]), publication)
                articles: list[dict[str, Any]] = []
                correlation_pairs: list[tuple[float, float]] = []
                for asset_id, publication in latest_publication_by_asset.items():
                    draft_id = publication["published_draft_id"]
                    draft = connection.execute("SELECT * FROM content_drafts WHERE id=? AND project_id=?", (draft_id, project_id)).fetchone() if draft_id is not None else None
                    if draft is None:
                        continue
                    authority_source_count = int(connection.execute("SELECT COUNT(*) FROM content_authority_source_links WHERE project_id=? AND content_asset_id=?", (project_id, asset_id)).fetchone()[0])
                    outline_section_count = int(connection.execute("SELECT COUNT(*) FROM content_outline_sections WHERE outline_id=?", (draft["outline_id"],)).fetchone()[0]) if draft["outline_id"] is not None else 0
                    quality_score, quality_breakdown = self._content_quality_score(draft, authority_source_count=authority_source_count, outline_section_count=outline_section_count)
                    snapshots = connection.execute(
                        """SELECT * FROM content_gsc_performance_snapshots WHERE project_id=? AND content_asset_id=?
                           ORDER BY collected_at DESC,id DESC LIMIT 2""",
                        (project_id, asset_id),
                    ).fetchall()
                    latest, previous = (snapshots[0], snapshots[1] if len(snapshots) > 1 else None) if snapshots else (None, None)
                    qualified = latest is not None and str(latest["learning_status"]) == "qualified"
                    performance_score: int | None = None
                    performance_breakdown: list[dict[str, Any]] = []
                    combined_score: int | None = None
                    if qualified and latest is not None:
                        performance_score, performance_breakdown = self._content_performance_score(latest, previous)
                        combined_score = round(quality_score * 0.6 + performance_score / 45 * 40)
                        correlation_pairs.append((float(quality_score), float(performance_score)))
                    used_rows = connection.execute(
                        """SELECT links.memory_id,links.role,links.relevance_score,links.selected_by_model,
                                  links.selected_by_user,links.created_at AS selected_at,
                                  memories.memory_type,memories.card_type,memories.topic,memories.source_url
                           FROM content_memory_links links
                           JOIN content_learning_memories memories ON memories.id=links.memory_id
                           WHERE links.content_asset_id=? AND memories.project_id=?
                             AND (links.selected_by_model=1 OR links.selected_by_user=1)
                             AND datetime(links.created_at) <= datetime(?)
                           ORDER BY links.relevance_score DESC,links.id""",
                        (asset_id, project_id, publication["published_at"]),
                    ).fetchall()
                    used_memories: list[dict[str, Any]] = []
                    for used in used_rows:
                        source_rows = connection.execute(
                            """SELECT source_type,source_id,source_url,source_content_hash,captured_at
                               FROM content_learning_memory_sources
                               WHERE project_id=? AND memory_id=? ORDER BY captured_at DESC,id DESC LIMIT 8""",
                            (project_id, used["memory_id"]),
                        ).fetchall()
                        selected_by = "model" if used["selected_by_model"] else "user"
                        used_memories.append({
                            "memory_id": int(used["memory_id"]),
                            "memory_type": used["memory_type"],
                            "card_type": used["card_type"],
                            "topic": used["topic"],
                            "source_url": used["source_url"],
                            "sources": [dict(source) for source in source_rows],
                            "relevance_score": float(used["relevance_score"]),
                            "selection_reason": f"{selected_by} selected this {used['role']} memory for the article before publication.",
                            "selected_at": used["selected_at"],
                        })
                    articles.append({
                        "content_asset_id": asset_id,
                        "draft_id": int(draft["id"]),
                        "title": publication["title_snapshot"],
                        "published_url": publication["wordpress_url"],
                        "quality_score": quality_score,
                        "quality_breakdown": quality_breakdown,
                        "performance_status": "qualified" if qualified else (str(latest["learning_status"]) if latest is not None else "observing"),
                        "performance_score": performance_score,
                        "performance_breakdown": performance_breakdown,
                        "combined_score": combined_score,
                        "latest_snapshot": dict(latest) if latest is not None else None,
                        "memory_assisted": bool(used_memories),
                        "evaluation_group": "memory_assisted" if used_memories else "no_memory_baseline",
                        "used_memories": used_memories,
                    })
                correlation = self._spearman_correlation(correlation_pairs)
                if correlation is None:
                    correlation_state = "insufficient"
                    correlation_note = f"目前只有 {len(correlation_pairs)} 篇文章具备至少两次、间隔 7 天且样本合格的 GSC 快照；达到 5 篇后才计算相关性。"
                else:
                    correlation_state = "descriptive"
                    correlation_note = "该系数只描述本项目文章质量检查分与当前 GSC 表现分的同向程度，不证明内容质量单独造成排名变化。"
        except (sqlite3.Error, ValueError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.OK, {
            "articles": articles,
            "summary": {
                "published_articles": len(articles),
                "qualified_articles": len(correlation_pairs),
                "memory_assisted_articles": sum(1 for article in articles if article["memory_assisted"]),
                "correlation_state": correlation_state,
                "spearman_correlation": correlation,
                "note": correlation_note,
            },
        })

    def _export_gsc_anchor_candidates(self, project_id: int) -> None:
        with self._database() as connection:
            project = connection.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
            rows = connection.execute("SELECT query,page_url,clicks,impressions,ctr,position,collected_at FROM project_gsc_query_rows WHERE project_id=? ORDER BY position ASC, impressions DESC, query ASC", (project_id,)).fetchall()
        if project is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "project does not exist"}); return
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["查询词", "对应网页 URL", "点击", "展示", "点击率", "平均排名", "采集时间"])
        for row in rows:
            writer.writerow([row["query"], row["page_url"], row["clicks"], row["impressions"], row["ctr"], row["position"], row["collected_at"]])
        raw = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(project["name"]) or "gsc").strip("-") or "gsc"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{safe_name}-gsc-query-page.csv"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)

    def _gsc_properties(self) -> list[str]:
        payload = self._gsc_request("GET", "https://searchconsole.googleapis.com/webmasters/v3/sites")
        return [str(item.get("siteUrl")) for item in payload.get("siteEntry", []) if item.get("siteUrl")]

    def _gsc_request(self, method: str, url: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        with self._credential_database() as connection: connection = connection.execute("SELECT refresh_token FROM gsc_oauth_credentials WHERE id=1").fetchone()
        if connection is None: raise ValueError("connect a Google account first")
        settings = _gsc_settings(self.server.ai_settings_path); refresh_token = self._unprotect_gsc_token(str(connection["refresh_token"]))
        token_response = requests.post("https://oauth2.googleapis.com/token", data={"client_id": settings["client_id"], "client_secret": settings["client_secret"], "refresh_token": refresh_token, "grant_type": "refresh_token"}, timeout=20); token_response.raise_for_status(); access_token = str(token_response.json().get("access_token") or "")
        response = requests.request(method, url, headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}, json=body, timeout=35); response.raise_for_status(); return response.json()

    @staticmethod
    def _protect_gsc_token(value: str) -> str:
        if win32crypt is None: raise RuntimeError("Windows credential encryption is unavailable")
        return "dpapi:" + base64.b64encode(win32crypt.CryptProtectData(value.encode("utf-8"), "SEO Control GSC", None, None, None, 0)).decode("ascii")

    @staticmethod
    def _unprotect_gsc_token(value: str) -> str:
        if not value.startswith("dpapi:") or win32crypt is None: raise RuntimeError("GSC token needs to be connected again on this Windows user account")
        return win32crypt.CryptUnprotectData(base64.b64decode(value.removeprefix("dpapi:")), None, None, None, 0)[1].decode("utf-8")

    def _get_wordpress_config(self, project_id: int) -> None:
        with self._credential_database() as connection:
            row = connection.execute("SELECT project_id,site_url,username,updated_at,last_tested_at FROM wordpress_credentials WHERE project_id=?", (project_id,)).fetchone()
        self._json(HTTPStatus.OK, dict(row) | {"configured": True} if row else {"configured": False})

    def _save_wordpress_config(self, project_id: int, payload: Mapping[str, Any]) -> None:
        site_url, username, password = self._text(payload, "site_url"), self._text(payload, "username"), self._text(payload, "password")
        if None in {site_url, username, password}: return
        site_url = self._normalize_wordpress_site_url(site_url)
        if not site_url.startswith(("https://", "http://")):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "site_url must start with https:// or http://."}); return
        try:
            with self._database() as connection:
                if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None: raise ValueError("project does not exist")
            with self._credential_database() as connection, connection:
                connection.execute("INSERT INTO wordpress_credentials(project_id,site_url,username,application_password) VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET site_url=excluded.site_url,username=excluded.username,application_password=excluded.application_password,last_tested_at=NULL,updated_at=CURRENT_TIMESTAMP", (project_id, site_url.rstrip("/"), username, self._protect_wordpress_password(password)))
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._get_wordpress_config(project_id)

    def _test_wordpress_config(self, project_id: int, payload: Mapping[str, Any]) -> None:
        try:
            configuration = self._wordpress_configuration(project_id, payload)
            session, _page = self._wordpress_admin_session(configuration)
            with self._credential_database() as connection, connection:
                connection.execute("UPDATE wordpress_credentials SET last_tested_at=CURRENT_TIMESTAMP WHERE project_id=?", (project_id,))
        except Exception as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": f"WordPress backend login failed: {type(error).__name__}. Check site URL, username and password."}); return
        self._json(HTTPStatus.OK, {"status": "connected", "username": configuration["username"], "method": "python_form_session"})

    def _prepare_wordpress_publish(self, asset_id: int, payload: Mapping[str, Any]) -> None:
        """Run a non-writing publication gate and create one explicit approval request.

        This endpoint deliberately never opens a WordPress session.  The gate
        report is durable so the UI can show exactly what the user approved,
        while the actual external write remains impossible without that
        approval being approved and consumed by ``_publish_wordpress``.
        """
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        requested_status = self._text(payload, "status") or "draft"
        if requested_status not in {"draft", "publish"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "WordPress status must be draft or publish"})
            return
        allow_without_images = payload.get("allow_without_images") is True
        try:
            with self._database() as connection, connection:
                asset = self._content_asset(connection, project_id, asset_id)
                report = self._wordpress_publish_gate_report(
                    connection, project_id, asset, requested_status, allow_without_images=allow_without_images,
                )
                cursor = connection.execute(
                    """INSERT INTO content_publish_gate_reports(project_id,content_asset_id,draft_id,requested_status,report_json,status)
                       VALUES(?,?,?,?,?,?)""",
                    (project_id, asset_id, report["draft_id"], requested_status, json.dumps(report, ensure_ascii=False), report["status"]),
                )
                report_id = int(cursor.lastrowid)
                response: dict[str, Any] = {"gate_report_id": report_id, "report": report, "status": report["status"]}
                if report["status"] == "ready":
                    pending = connection.execute(
                        """SELECT approvals.id,approvals.job_id,approvals.payload_json
                           FROM agent_approval_requests approvals
                           JOIN agent_jobs jobs ON jobs.id=approvals.job_id
                           WHERE approvals.project_id=? AND approvals.approval_type='publish'
                             AND approvals.status='pending' AND jobs.content_asset_id=?
                             AND jobs.status='waiting_approval'
                           ORDER BY approvals.id DESC""",
                        (project_id, asset_id),
                    ).fetchall()
                    for approval in pending:
                        try:
                            approval_payload = json.loads(approval["payload_json"] or "{}")
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if (
                            isinstance(approval_payload, Mapping)
                            and approval_payload.get("draft_id") == report["draft_id"]
                            and approval_payload.get("requested_status") == requested_status
                        ):
                            approval_payload = dict(approval_payload)
                            approval_payload["gate_report_id"] = report_id
                            approval_payload["report"] = report
                            connection.execute(
                                "UPDATE agent_approval_requests SET payload_json=? WHERE id=?",
                                (json.dumps(approval_payload, ensure_ascii=False), approval["id"]),
                            )
                            response |= {
                                "job_id": int(approval["job_id"]),
                                "approval_id": int(approval["id"]),
                                "reused": True,
                            }
                            self._json(HTTPStatus.OK, response)
                            return
                    workflow = run_content_workflow_skeleton(project_id=project_id, content_asset_id=asset_id, requested_action="prepare_publish")
                    job_cursor = connection.execute(
                        """INSERT INTO agent_jobs(project_id,content_asset_id,requested_action,status,current_node,input_json)
                           VALUES(?,?,?,'waiting_approval',?,?)""",
                        (project_id, asset_id, "prepare_publish", str(workflow["current_node"]), json.dumps({
                            "requested_action": "prepare_publish", "content_asset_id": asset_id,
                            "allowed_tools": workflow["allowed_tools"], "gate_report_id": report_id,
                        }, ensure_ascii=False)),
                    )
                    job_id = int(job_cursor.lastrowid)
                    for event in workflow["events"]:
                        connection.execute(
                            """INSERT INTO agent_steps(job_id,node_name,status,input_summary,output_json,started_at,completed_at)
                               VALUES(?,?, 'completed', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                            (job_id, event["node"], event["message"], json.dumps({"message": event["message"]}, ensure_ascii=False)),
                        )
                    approval_payload = {
                        "content_asset_id": asset_id,
                        "draft_id": report["draft_id"],
                        "requested_status": requested_status,
                        "gate_report_id": report_id,
                        "report": report,
                    }
                    approval_cursor = connection.execute(
                        "INSERT INTO agent_approval_requests(project_id,job_id,approval_type,payload_json) VALUES(?,?, 'publish', ?)",
                        (project_id, job_id, json.dumps(approval_payload, ensure_ascii=False)),
                    )
                    response |= {"job_id": job_id, "approval_id": int(approval_cursor.lastrowid)}
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, response)

    def _wordpress_publish_gate_report(
        self, connection: sqlite3.Connection, project_id: int, asset: sqlite3.Row, requested_status: str, *, allow_without_images: bool,
    ) -> dict[str, Any]:
        """Return a deterministic, credential-free readiness report for a draft."""
        draft = connection.execute("SELECT * FROM content_drafts WHERE id=? AND project_id=? AND content_asset_id=?", (asset["current_draft_id"], project_id, asset["id"])).fetchone() if asset["current_draft_id"] else None
        issues: list[dict[str, str]] = []
        checks: list[dict[str, Any]] = []

        def check(code: str, passed: bool, message: str, *, blocking: bool = True) -> None:
            checks.append({"code": code, "passed": passed, "message": message})
            if blocking and not passed:
                issues.append({"code": code, "message": message})

        check("current_draft", draft is not None, "当前内容没有可发布的正文版本。")
        if draft is None:
            return {"status": "blocked", "draft_id": None, "requested_status": requested_status, "checks": checks, "issues": issues}
        check("qa", str(draft["qa_status"]) == "approved", "请先在内容页完成 AI 质量审核，并确保审核结果为“通过”。")
        try:
            unresolved = json.loads(draft["unresolved_verify_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            unresolved = ["invalid verification state"]
        check("verification", not unresolved, "正文仍有待验证事项；请修订或确认后重新进行质量审核。")
        title = str(draft["title"] or "").strip()
        meta_description = str(draft["meta_description"] or "").strip()
        check("title", bool(title), "正文标题为空，无法创建 WordPress 文章。")
        check(
            "meta_description",
            50 <= len(meta_description) <= 180,
            "Meta Description 必须为 50–180 个字符，避免搜索摘要缺失或被截断。",
        )
        try:
            tags = json.loads(asset["tags_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            tags = []
        check("tags", isinstance(tags, list) and 2 <= len(tags) <= 3 and all(isinstance(tag, str) and tag.strip() for tag in tags), "内容必须保留 2–3 个有效英文标签。")
        with self._credential_database() as credential_connection:
            configuration = credential_connection.execute("SELECT last_tested_at FROM wordpress_credentials WHERE project_id=?", (project_id,)).fetchone()
        check("wordpress_connection", configuration is not None and configuration["last_tested_at"] is not None, "请先在“内容发布”中保存并测试当前网站的 WordPress 后台连接。")
        images = connection.execute("SELECT seo_filename,status FROM content_section_images WHERE project_id=? AND content_asset_id=? AND draft_id=? ORDER BY position", (project_id, asset["id"], draft["id"])).fetchall()
        if not images:
            check("images", allow_without_images, "当前正文没有 H2 配图；请生成配图，或明确确认无图发布。")
        else:
            image_failures = []
            for image in images:
                filename = str(image["seo_filename"] or "")
                file_path = WEB_ROOT / "generated-images" / str(project_id) / filename
                if image["status"] != "ready" or not filename or not file_path.is_file() or file_path.stat().st_size <= 0:
                    image_failures.append(filename or "未命名配图")
            check("images", not image_failures, f"以下 H2 配图尚未成功生成或本地文件不可读：{', '.join(image_failures[:3])}。" if image_failures else "H2 配图均已生成且本地文件可读。")
        markdown = str(draft["markdown"] or "")
        markdown_links = re.findall(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)", markdown)
        unsafe_links = [
            target for target in markdown_links
            if not target.startswith(("https://", "http://", "/", "#"))
        ]
        check(
            "links",
            not unsafe_links,
            f"正文包含不安全或不受支持的链接：{', '.join(unsafe_links[:3])}。" if unsafe_links else "正文链接协议检查通过。",
        )
        duplicate = connection.execute(
            """SELECT publications.content_asset_id
               FROM content_wordpress_publications publications
               JOIN content_drafts published_drafts ON published_drafts.id=publications.draft_id
               WHERE publications.project_id=? AND publications.status='publish'
                 AND publications.content_asset_id<>? AND TRIM(published_drafts.markdown)=TRIM(?)
               LIMIT 1""",
            (project_id, asset["id"], markdown),
        ).fetchone()
        check("duplicate", duplicate is None, "当前正文与本项目另一篇已公开文章完全重复，请先处理重复内容风险。")
        markdown_has_table = bool(re.search(r"^\s*\|.+\|\s*$", markdown, flags=re.MULTILINE))
        rendered_html = self._markdown_to_wordpress_html(markdown)
        check("tables", not markdown_has_table or "<table" in rendered_html, "正文中的 Markdown 表格无法转换为 WordPress HTML 表格。")
        check("html", bool(rendered_html.strip()), "正文无法转换为可发布的 HTML。")
        return {
            "status": "ready" if not issues else "blocked", "draft_id": int(draft["id"]), "requested_status": requested_status,
            "allow_without_images": allow_without_images, "checks": checks, "issues": issues,
        }

    def _consume_publish_approval(self, connection: sqlite3.Connection, project_id: int, asset_id: int, status: str, approval_id: int) -> None:
        """Bind a one-time approved action to the exact current draft and gate report."""
        approval = connection.execute(
            """SELECT approvals.*,jobs.content_asset_id FROM agent_approval_requests approvals
               JOIN agent_jobs jobs ON jobs.id=approvals.job_id
               WHERE approvals.id=? AND approvals.project_id=? AND approvals.approval_type='publish'""",
            (approval_id, project_id),
        ).fetchone()
        if approval is None or approval["status"] != "approved" or approval["consumed_at"] is not None:
            raise ValueError("a current approved WordPress publish request is required")
        try:
            approval_payload = json.loads(approval["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            raise ValueError("publish approval payload is invalid")
        if not isinstance(approval_payload, Mapping) or approval["content_asset_id"] != asset_id:
            raise ValueError("publish approval does not belong to this content")
        asset = self._content_asset(connection, project_id, asset_id)
        draft_id = asset["current_draft_id"]
        if approval_payload.get("content_asset_id") != asset_id or approval_payload.get("draft_id") != draft_id or approval_payload.get("requested_status") != status:
            raise ValueError("publish approval is for a different content version or status")
        gate_report_id = approval_payload.get("gate_report_id")
        report_row = connection.execute("SELECT * FROM content_publish_gate_reports WHERE id=? AND project_id=? AND content_asset_id=? AND draft_id=? AND requested_status=?", (gate_report_id, project_id, asset_id, draft_id, status)).fetchone()
        if report_row is None:
            raise ValueError("publish approval does not have a matching gate report")
        report = self._wordpress_publish_gate_report(connection, project_id, asset, status, allow_without_images=bool(json.loads(report_row["report_json"] or "{}").get("allow_without_images")))
        if report["status"] != "ready":
            raise ValueError("publish gate no longer passes: " + " ".join(issue["message"] for issue in report["issues"]))
        cursor = connection.execute("UPDATE agent_approval_requests SET consumed_at=CURRENT_TIMESTAMP WHERE id=? AND status='approved' AND consumed_at IS NULL", (approval_id,))
        if cursor.rowcount != 1:
            raise ValueError("publish approval has already been consumed")

    def _record_publish_job_outcome(
        self,
        project_id: int,
        approval_id: int,
        *,
        succeeded: bool,
        message: str,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        """Keep the durable approval job truthful without storing WordPress secrets."""
        with self._database() as connection, connection:
            approval = connection.execute("SELECT job_id FROM agent_approval_requests WHERE id=? AND project_id=?", (approval_id, project_id)).fetchone()
            if approval is None:
                return
            job_status = "completed" if succeeded else "failed"
            node = "wordpress_publish_completed" if succeeded else "wordpress_publish_failed"
            connection.execute(
                """UPDATE agent_jobs SET status=?,current_node=?,error_summary=?,result_json=?,
                       completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    job_status,
                    node,
                    None if succeeded else message[:500],
                    json.dumps(dict(result or {}), ensure_ascii=False),
                    approval["job_id"],
                ),
            )
            connection.execute(
                """INSERT INTO agent_steps(job_id,node_name,status,input_summary,output_json,started_at,completed_at)
                   VALUES(?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                (approval["job_id"], node, "completed" if succeeded else "failed", message[:500], json.dumps({"approval_id": approval_id}, ensure_ascii=False)),
            )

    def _publish_wordpress(self, asset_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None: return
        status = self._text(payload, "status") or "draft"
        approval_id = self._integer(payload, "approval_id")
        if status not in {"draft", "publish"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "WordPress status must be draft or publish"}); return
        if approval_id is None:
            self._json(HTTPStatus.FORBIDDEN, {"error": "请先通过发布门禁并完成一次人工确认。"}); return
        try:
            with self._database() as gate_connection, gate_connection:
                self._consume_publish_approval(gate_connection, project_id, asset_id, status, approval_id)
                approval = gate_connection.execute("SELECT job_id FROM agent_approval_requests WHERE id=?", (approval_id,)).fetchone()
                if approval is not None:
                    gate_connection.execute("UPDATE agent_jobs SET status='running',current_node='wordpress_publish_started',updated_at=CURRENT_TIMESTAMP WHERE id=?", (approval["job_id"],))
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        wordpress_stage = {"value": "create_draft"}
        try:
            with self._database() as connection:
                asset = self._content_asset(connection, project_id, asset_id)
                draft = connection.execute("SELECT * FROM content_drafts WHERE id=?", (asset["current_draft_id"],)).fetchone() if asset["current_draft_id"] else None
                if draft is None: raise ValueError("a completed article is required before publishing")
                configuration = self._wordpress_configuration(project_id, payload)
                session, editor = self._wordpress_admin_session(configuration)
                if not self._wordpress_has_classic_post_form(editor):
                    post_id, link = self._publish_wordpress_gutenberg(
                        connection, project_id, asset_id, draft, session, configuration, status, editor,
                        lambda stage: wordpress_stage.__setitem__("value", stage),
                    )
                    with connection:
                        cursor = connection.execute("INSERT INTO content_wordpress_publications(project_id,content_asset_id,draft_id,wordpress_post_id,wordpress_url,status) VALUES(?,?,?,?,?,?)", (project_id, asset_id, draft["id"], post_id, link, status))
                        row = connection.execute("SELECT * FROM content_wordpress_publications WHERE id=?", (cursor.lastrowid,)).fetchone()
                    self._record_publish_job_outcome(
                        project_id,
                        approval_id,
                        succeeded=True,
                        message="WordPress publication completed.",
                        result=dict(row),
                    )
                    self._json(HTTPStatus.CREATED, dict(row)); return
                nonce = self._wordpress_post_nonce(editor)
                if not nonce: raise ValueError("WordPress post form did not contain _wpnonce")
                # A media attachment may only be associated with an existing
                # WordPress post.  Create a real draft first, then attach the
                # locally generated H2 images to that post, and finally update
                # the draft to the selected status.
                initial_html = self._markdown_to_wordpress_html(str(draft["markdown"]))
                wordpress_stage["value"] = "create_draft"
                create_form = self._wordpress_post_form(
                    nonce, post_id=0, original_status="auto-draft", status="draft",
                    title=str(draft["title"]), content=initial_html,
                    excerpt=str(draft["meta_description"] or ""),
                )
                session.headers.update({"Referer": f"{configuration['site_url']}/wp-admin/post-new.php", "Origin": configuration["site_url"]})
                create_response = session.post(f"{configuration['site_url']}/wp-admin/post.php", data=create_form, timeout=40, allow_redirects=True)
                # A post-new page can contain several WordPress nonces.  Only
                # the nonce inside the article form is valid for editpost. If
                # the server still reports an expired link, refresh it once.
                if self._wordpress_link_expired(create_response):
                    refreshed_editor = session.get(f"{configuration['site_url']}/wp-admin/post-new.php?post_type=post", timeout=30)
                    refreshed_editor.raise_for_status()
                    refreshed_nonce = self._wordpress_post_nonce(refreshed_editor.text)
                    if not refreshed_nonce:
                        raise ValueError("WordPress refreshed the post editor but did not provide its form nonce")
                    create_form = self._wordpress_post_form(
                        refreshed_nonce, post_id=0, original_status="auto-draft", status="draft",
                        title=str(draft["title"]), content=initial_html,
                        excerpt=str(draft["meta_description"] or ""),
                    )
                    create_response = session.post(f"{configuration['site_url']}/wp-admin/post.php", data=create_form, timeout=40, allow_redirects=True)
                create_response.raise_for_status()
                post_id = self._wordpress_post_id(create_response.url, create_response.text)
                if post_id is None: raise ValueError("WordPress did not confirm a created post ID")
                editor_response = session.get(f"{configuration['site_url']}/wp-admin/post.php?post={post_id}&action=edit", timeout=30)
                editor_response.raise_for_status()
                update_nonce = self._wordpress_post_nonce(editor_response.text)
                if not update_nonce: raise ValueError("WordPress saved the draft but did not provide an edit nonce")
                wordpress_stage["value"] = "upload_media"
                article_html = self._wordpress_article_html(connection, project_id, asset_id, draft, session, configuration, post_id)
                wordpress_stage["value"] = "publish" if status == "publish" else "save_draft"
                update_form = self._wordpress_post_form(
                    update_nonce, post_id=post_id, original_status="draft", status=status,
                    title=str(draft["title"]), content=article_html,
                    excerpt=str(draft["meta_description"] or ""),
                )
                session.headers.update({"Referer": f"{configuration['site_url']}/wp-admin/post.php?post={post_id}&action=edit", "Origin": configuration["site_url"]})
                response = session.post(f"{configuration['site_url']}/wp-admin/post.php", data=update_form, timeout=40, allow_redirects=True)
                response.raise_for_status()
                link = self._wordpress_public_url(session, configuration["site_url"], post_id, response.text) if status == "publish" else f"{configuration['site_url']}/wp-admin/post.php?post={post_id}&action=edit"
                with connection:
                    cursor = connection.execute("INSERT INTO content_wordpress_publications(project_id,content_asset_id,draft_id,wordpress_post_id,wordpress_url,status) VALUES(?,?,?,?,?,?)", (project_id, asset_id, draft["id"], post_id, link if isinstance(link, str) else None, status))
                    row = connection.execute("SELECT * FROM content_wordpress_publications WHERE id=?", (cursor.lastrowid,)).fetchone()
        except requests.HTTPError as error:
            response = error.response
            if response is not None and response.status_code == HTTPStatus.FORBIDDEN:
                detail = self._wordpress_error_summary(response.text)
                hints = {
                    "create_draft": "登录成功，但该账号没有创建文章权限；请授予 Author、Editor 或 Administrator（edit_posts）。",
                    "upload_media": "草稿已创建，但该账号没有上传媒体库权限；请授予 Author、Editor 或 Administrator（upload_files）。",
                    "save_draft": "该账号不能编辑刚创建的文章；请检查是否拥有 edit_posts / edit_post 权限。",
                    "publish": "文章与配图已处理，但该账号不能公开发布；请授予 Author、Editor 或 Administrator（publish_posts）。",
                }
                stage_names = {"create_draft": "创建草稿", "upload_media": "上传本地配图", "save_draft": "保存草稿", "publish": "公开发布"}
                stage = wordpress_stage["value"]
                expired_hint = "WordPress 的文章编辑令牌已过期；系统已自动刷新并重试一次，但仍被拒绝。请重新保存 WordPress 连接配置后再试。" if self._wordpress_link_expired(response) else hints.get(stage, "")
                message = f"WordPress 在“{stage_names.get(stage, '发布')}”步骤拒绝了请求（403）。{expired_hint}{' WordPress 提示：' + detail if detail else ''}"
                self._record_publish_job_outcome(project_id, approval_id, succeeded=False, message=message)
                self._json(HTTPStatus.BAD_GATEWAY, {"error": message}); return
            message = f"WordPress publishing failed: HTTP {response.status_code if response is not None else 'error'}: {str(error)[:220]}"
            self._record_publish_job_outcome(project_id, approval_id, succeeded=False, message=message)
            self._json(HTTPStatus.BAD_GATEWAY, {"error": message}); return
        except Exception as error:
            message = f"WordPress publishing failed: {type(error).__name__}: {str(error)[:300]}"
            self._record_publish_job_outcome(project_id, approval_id, succeeded=False, message=message)
            self._json(HTTPStatus.BAD_GATEWAY, {"error": message}); return
        self._record_publish_job_outcome(
            project_id,
            approval_id,
            succeeded=True,
            message="WordPress publication completed.",
            result=dict(row),
        )
        self._json(HTTPStatus.CREATED, dict(row))

    def _publish_wordpress_gutenberg(self, connection: sqlite3.Connection, project_id: int, asset_id: int, draft: sqlite3.Row, session: requests.Session, configuration: Mapping[str, str], status: str, editor_html: str, set_stage: Any) -> tuple[int, str]:
        """Publish through WordPress's authenticated Gutenberg API when no classic post form exists."""
        api = self._wordpress_gutenberg_api(editor_html, configuration["site_url"])
        if api is None:
            raise ValueError("WordPress uses the block editor but did not provide an authenticated editor API token")
        headers = {"X-WP-Nonce": api["nonce"], "Referer": f"{configuration['site_url']}/wp-admin/post-new.php", "Origin": configuration["site_url"]}
        initial_html = self._markdown_to_wordpress_html(str(draft["markdown"]))
        existing_publication = connection.execute(
            "SELECT wordpress_post_id FROM content_wordpress_publications WHERE project_id=? AND content_asset_id=? AND status='publish' AND wordpress_post_id IS NOT NULL ORDER BY id DESC LIMIT 1",
            (project_id, asset_id),
        ).fetchone()
        # A direct re-publish is an update to this article's existing public
        # page.  It prevents a formatting fix from creating a second URL with
        # the same title, while draft publishing remains non-destructive.
        if status == "publish" and existing_publication is not None:
            post_id = int(existing_publication["wordpress_post_id"])
            set_stage("upload_media")
            article_html = self._wordpress_article_html(connection, project_id, asset_id, draft, session, configuration, post_id)
            set_stage("publish")
            update = session.post(f"{api['root']}posts/{post_id}", json={"title": str(draft["title"]), "content": article_html, "excerpt": str(draft["meta_description"] or ""), "status": "publish"}, headers=headers, timeout=40)
            update.raise_for_status()
            updated = update.json()
            public_link = updated.get("link") if isinstance(updated, Mapping) else None
            return post_id, public_link if isinstance(public_link, str) else f"{configuration['site_url']}/?p={post_id}"
        set_stage("create_draft")
        create = session.post(f"{api['root']}posts", json={"title": str(draft["title"]), "content": initial_html, "excerpt": str(draft["meta_description"] or ""), "status": "draft"}, headers=headers, timeout=40)
        create.raise_for_status()
        created = create.json()
        post_id = created.get("id") if isinstance(created, Mapping) else None
        if not isinstance(post_id, int):
            raise ValueError("WordPress Gutenberg API did not return a created post ID")
        set_stage("upload_media")
        article_html = self._wordpress_article_html(connection, project_id, asset_id, draft, session, configuration, post_id)
        set_stage("publish" if status == "publish" else "save_draft")
        update = session.post(f"{api['root']}posts/{post_id}", json={"title": str(draft["title"]), "content": article_html, "excerpt": str(draft["meta_description"] or ""), "status": status}, headers=headers, timeout=40)
        update.raise_for_status()
        updated = update.json()
        public_link = updated.get("link") if isinstance(updated, Mapping) else None
        link = public_link if status == "publish" and isinstance(public_link, str) else f"{configuration['site_url']}/wp-admin/post.php?post={post_id}&action=edit"
        return post_id, link

    @staticmethod
    def _wordpress_gutenberg_api(editor_html: str, site_url: str) -> dict[str, str] | None:
        match = re.search(r"wpApiSettings\s*=\s*({.*?});", editor_html, flags=re.DOTALL)
        if match is None:
            return None
        try:
            settings = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        root, nonce, version = settings.get("root"), settings.get("nonce"), settings.get("versionString")
        if not isinstance(root, str) or not isinstance(nonce, str) or not root or not nonce:
            return None
        api_root = urljoin(f"{site_url.rstrip('/')}/", root)
        if isinstance(version, str) and version:
            api_root = urljoin(api_root.rstrip("/") + "/", version)
        return {"root": api_root.rstrip("/") + "/", "nonce": nonce}

    @staticmethod
    def _wordpress_has_classic_post_form(editor_html: str) -> bool:
        return re.search(r"<form\b[^>]*\bid=[\"']post[\"']", editor_html, flags=re.IGNORECASE) is not None

    @staticmethod
    def _wordpress_post_form(nonce: str, *, post_id: int, original_status: str, status: str, title: str, content: str, excerpt: str) -> dict[str, str]:
        """Build a classic-editor post form for an initial draft or final update."""
        form = {
            "action": "editpost", "_wpnonce": nonce,
            "_wp_http_referer": "/wp-admin/post-new.php" if post_id == 0 else f"/wp-admin/post.php?post={post_id}&action=edit",
            "post_ID": str(post_id), "post_type": "post", "original_post_status": original_status,
            "post_status": status, "post_title": title, "content": content, "excerpt": excerpt,
            "save": "Publish" if status == "publish" else "Save Draft",
            "post_author": "0", "post_password": "", "visibility": "public",
        }
        if status == "publish":
            form["publish"] = "Publish"
        return form

    @staticmethod
    def _wordpress_error_summary(page_html: str) -> str:
        """Extract WordPress's human error text without leaking its page CSS."""
        clean_html = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", page_html, flags=re.IGNORECASE | re.DOTALL)
        paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", clean_html, flags=re.IGNORECASE | re.DOTALL)
        source = " ".join(paragraphs) if paragraphs else clean_html
        text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", source))).strip()
        return text[:260]

    @staticmethod
    def _wordpress_link_expired(response: requests.Response) -> bool:
        if response.status_code != HTTPStatus.FORBIDDEN:
            return False
        text = KeywordDiscoveryRequestHandler._wordpress_error_summary(response.text).lower()
        return "链接已过期" in text or "link you followed has expired" in text or "nonce" in text

    def _wordpress_article_html(self, connection: sqlite3.Connection, project_id: int, asset_id: int, draft: sqlite3.Row, session: requests.Session, configuration: Mapping[str, str], post_id: int) -> str:
        """Upload generated local H2 images to WordPress and embed their media URLs."""
        article_html = self._markdown_to_wordpress_html(str(draft["markdown"]))
        images = connection.execute(
            "SELECT section_heading,alt_text,seo_filename,status FROM content_section_images WHERE project_id=? AND content_asset_id=? AND draft_id=? ORDER BY position",
            (project_id, asset_id, draft["id"]),
        ).fetchall()
        for image in images:
            if str(image["status"]) != "ready" or not image["seo_filename"]:
                continue
            file_path = WEB_ROOT / "generated-images" / str(project_id) / str(image["seo_filename"])
            if not file_path.is_file() or file_path.stat().st_size <= 0:
                raise ValueError(f"local article image is missing: {image['seo_filename']}")
            media_url = self._wordpress_upload_media(session, configuration["site_url"], file_path, post_id)
            heading = html.escape(str(image["section_heading"]))
            alt_text = html.escape(str(image["alt_text"] or image["section_heading"]), quote=True)
            figure = f'<figure class="wp-block-image size-large seo-control-section-image" style="width:100%;max-width:800px;margin:24px auto;"><img src="{html.escape(media_url, quote=True)}" alt="{alt_text}" width="800" height="600" loading="lazy" style="display:block;width:100%;max-width:800px;height:auto;object-fit:contain;" /><figcaption style="display:flex;align-items:flex-start;gap:8px;margin-top:9px;color:#64748b;font-size:12px;line-height:1.5;"><span style="display:inline-block;flex:0 0 auto;padding:2px 6px;border-radius:999px;background:#e8f4f1;color:#176b5a;font-size:10px;font-weight:700;letter-spacing:.06em;line-height:1.35;">IMAGE</span><span>{alt_text}</span></figcaption></figure>'
            article_html = article_html.replace(f"<h2>{heading}</h2>", f"<h2>{heading}</h2>{figure}", 1)
        references = self._wordpress_authority_references(connection, project_id, asset_id)
        if references:
            items = "".join(
                f'<li><a href="{html.escape(reference["url"], quote=True)}" target="_blank" rel="nofollow noopener noreferrer">{html.escape(reference["title"])}</a><span> — {html.escape(reference["publisher"])}</span></li>'
                for reference in references
            )
            article_html += f'\n<section class="seo-control-authority-references"><h2>Authoritative References</h2><ul>{items}</ul></section>'
        return article_html

    @staticmethod
    def _authority_reference_is_relevant(source: Mapping[str, Any], *, keyword: str, article_title: str) -> bool:
        """Require an exact, section-level topic match before rendering a footer citation."""
        section_heading = str(source.get("section_heading") or "").strip()
        claim_topic = str(source.get("claim_topic") or "").strip()
        if not section_heading and not claim_topic:
            # Sources attached only to a broad Brief were never verified against
            # a reader-facing section. They must not become public citations.
            return False
        source_title = str(source.get("title") or "").casefold()
        topic = f"{keyword} {article_title} {section_heading} {claim_topic}".casefold()
        ignored = {"about", "article", "best", "check", "complete", "guide", "ideas", "inspiring", "light", "lights", "outdoor", "reference", "source", "stair", "stairs", "the", "this", "with"}
        terms = {
            item for item in re.findall(r"[a-z][a-z0-9-]*", topic)
            if len(item) >= 3 and item not in ignored
        }
        matches = {term for term in terms if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", source_title)}
        led_equivalent = ("led" in topic or "lighting" in topic) and ("led" in source_title or "light-emitting diode" in source_title)
        ip_equivalent = ("ip" in topic or "ingress protection" in topic) and ("ip code" in source_title or "ingress protection" in source_title)
        return len(matches) >= 2 or (led_equivalent and len(matches) >= 1) or ip_equivalent

    def _wordpress_authority_references(self, connection: sqlite3.Connection, project_id: int, asset_id: int) -> list[dict[str, str]]:
        rows = connection.execute(
            """SELECT sources.title,sources.url,sources.publisher,links.section_heading,links.claim_topic,assets.title_snapshot,keywords.keyword
               FROM content_authority_source_links links
               JOIN authority_source_library sources ON sources.id=links.authority_source_id
               JOIN content_assets assets ON assets.id=links.content_asset_id
               JOIN keywords ON keywords.id=assets.keyword_id
               WHERE links.project_id=? AND links.content_asset_id=? AND sources.url IS NOT NULL
                 AND sources.authority_level IN ('primary','authoritative')
               ORDER BY CASE sources.authority_level WHEN 'primary' THEN 0 WHEN 'authoritative' THEN 1 ELSE 2 END,links.id DESC""",
            (project_id, asset_id),
        ).fetchall()
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            source = dict(row); url = str(source.get("url") or "").strip()
            if not url.startswith(("https://", "http://")) or url in seen or not self._authority_reference_is_relevant(source, keyword=str(source.get("keyword") or ""), article_title=str(source.get("title_snapshot") or "")):
                continue
            seen.add(url)
            result.append({"title": str(source.get("title") or "Reference"), "url": url, "publisher": str(source.get("publisher") or "Source")})
            if len(result) >= 5:
                break
        return result

    @staticmethod
    def _wordpress_upload_media(session: requests.Session, site_url: str, file_path: Path, post_id: int) -> str:
        """Use the authenticated WordPress admin uploader; no REST API is used."""
        media_page = session.get(f"{site_url}/wp-admin/media-new.php", timeout=30)
        media_page.raise_for_status()
        nonce = KeywordDiscoveryRequestHandler._wordpress_hidden_value(media_page.text, "_wpnonce")
        if not nonce:
            raise ValueError("WordPress media uploader did not provide an upload nonce")
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        with file_path.open("rb") as image_file:
            upload = session.post(
                f"{site_url}/wp-admin/async-upload.php",
                data={"action": "upload-attachment", "_wpnonce": nonce, "post_id": str(post_id)},
                files={"async-upload": (file_path.name, image_file, mime_type)},
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{site_url}/wp-admin/media-new.php"},
                timeout=90,
            )
        upload.raise_for_status()
        try:
            payload = upload.json()
        except ValueError as error:
            raise ValueError(f"WordPress media upload returned invalid data: {upload.text[:160]}") from error
        data = payload.get("data") if isinstance(payload, Mapping) else None
        media_url = data.get("url") if isinstance(data, Mapping) else None
        if not isinstance(media_url, str) or not media_url.startswith(("http://", "https://")):
            reason = data.get("message") if isinstance(data, Mapping) else None
            raise ValueError(f"WordPress media upload did not return a URL{': ' + str(reason)[:160] if reason else ''}")
        return media_url

    def _wordpress_configuration(self, project_id: int, payload: Mapping[str, Any]) -> dict[str, str]:
        with self._credential_database() as connection:
            row = connection.execute("SELECT * FROM wordpress_credentials WHERE project_id=?", (project_id,)).fetchone()
        if row is None: raise ValueError("save this website's WordPress URL, username and backend password first")
        return {"site_url": row["site_url"], "username": row["username"], "password": self._unprotect_wordpress_password(row["application_password"])}

    @staticmethod
    def _normalize_wordpress_site_url(site_url: str) -> str:
        """Accept a site homepage, wp-admin URL, or wp-login URL and store the site root."""
        value = site_url.strip().rstrip("/")
        try:
            parsed = urlsplit(value)
        except ValueError:
            return value
        if not parsed.scheme or not parsed.netloc:
            return value
        path = parsed.path.rstrip("/")
        for suffix in ("/wp-admin", "/wp-login.php"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        return f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")

    @staticmethod
    def _wordpress_admin_session(configuration: Mapping[str, str]) -> tuple[requests.Session, str]:
        session = requests.Session(); session.headers.update({"User-Agent": "SEOControlPublisher/1.0", "Referer": f"{configuration['site_url']}/wp-login.php"})
        # WordPress requires its test cookie to be issued from the login page
        # before it accepts account credentials in this server-side session.
        login_page = session.get(f"{configuration['site_url']}/wp-login.php", timeout=30)
        login_page.raise_for_status()
        login = session.post(f"{configuration['site_url']}/wp-login.php", data={"log": configuration["username"], "pwd": configuration["password"], "wp-submit": "Log In", "redirect_to": f"{configuration['site_url']}/wp-admin/", "testcookie": "1"}, timeout=30, allow_redirects=True)
        login.raise_for_status()
        if "wp-login.php" in login.url or "login_error" in login.text:
            raise ValueError("WordPress rejected the backend login")
        editor = session.get(f"{configuration['site_url']}/wp-admin/post-new.php?post_type=post", timeout=30)
        editor.raise_for_status()
        if "_wpnonce" not in editor.text:
            raise ValueError("WordPress account cannot access the post editor")
        return session, editor.text

    @staticmethod
    def _wordpress_hidden_value(html_text: str, name: str) -> str | None:
        match = re.search(rf"<input[^>]+name=[\"']{re.escape(name)}[\"'][^>]+value=[\"']([^\"']+)", html_text, flags=re.IGNORECASE)
        return html.unescape(match.group(1)) if match else None

    @staticmethod
    def _wordpress_post_nonce(editor_html: str) -> str | None:
        """Read the nonce from the actual post form, not another admin widget."""
        form = re.search(r"<form\b[^>]*\bid=[\"']post[\"'][^>]*>(.*?)</form>", editor_html, flags=re.IGNORECASE | re.DOTALL)
        return KeywordDiscoveryRequestHandler._wordpress_hidden_value(form.group(1), "_wpnonce") if form else None

    @staticmethod
    def _wordpress_post_id(url: str, html_text: str) -> int | None:
        match = re.search(r"[?&]post=(\d+)", url) or re.search(r"name=[\"']post_ID[\"'][^>]+value=[\"'](\d+)", html_text, flags=re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _wordpress_public_url(session: requests.Session, site_url: str, post_id: int, editor_html: str) -> str:
        """Return the canonical public post URL after WordPress confirms a publish."""
        view_link = re.search(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]+id=[\"']view-post-btn[\"']", editor_html, flags=re.IGNORECASE)
        if view_link is None:
            view_link = re.search(r"<a[^>]+id=[\"']view-post-btn[\"'][^>]+href=[\"']([^\"']+)[\"']", editor_html, flags=re.IGNORECASE)
        if view_link is not None:
            return urljoin(f"{site_url}/", html.unescape(view_link.group(1)))
        fallback = f"{site_url}/?p={post_id}"
        try:
            page = session.get(fallback, timeout=30, allow_redirects=True)
            if page.ok and "/wp-login.php" not in page.url:
                return page.url
        except requests.RequestException:
            pass
        return fallback

    @staticmethod
    def _protect_wordpress_password(password: str) -> str:
        if win32crypt is None: raise RuntimeError("Windows credential encryption is unavailable")
        encrypted = win32crypt.CryptProtectData(password.encode("utf-8"), "SEO Control WordPress", None, None, None, 0)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")

    @staticmethod
    def _unprotect_wordpress_password(value: str) -> str:
        if not value.startswith("dpapi:") or win32crypt is None: raise RuntimeError("WordPress password needs to be saved again on this Windows user account")
        return win32crypt.CryptUnprotectData(base64.b64decode(value.removeprefix("dpapi:")), None, None, None, 0)[1].decode("utf-8")

    @staticmethod
    def _markdown_to_wordpress_html(markdown: str) -> str:
        """Render the supported writer Markdown as safe, semantic WordPress HTML.

        The writer deliberately uses Markdown tables, links and emphasis for SEO
        content.  Escaping every line turns all of those into visible syntax in
        WordPress, so parse that compact subset rather than relying on a theme.
        """
        # The verification record belongs to the internal quality gate.  Even
        # legacy drafts that contained an old marker must never expose it on a
        # public WordPress page.
        markdown = KeywordDiscoveryRequestHandler._sanitize_reader_markdown(markdown)
        lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        output: list[str] = []

        def inline(value: str) -> str:
            escaped = html.escape(value, quote=False)
            escaped = re.sub(
                r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                lambda match: f'<a href="{html.escape(html.unescape(match.group(2)), quote=True)}" target="_blank" rel="noopener noreferrer">{match.group(1)}</a>',
                escaped,
            )
            escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
            escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
            return escaped

        def cells(value: str) -> list[str]:
            trimmed = value.strip().strip("|")
            return [item.replace(r"\|", "|").strip() for item in re.split(r"(?<!\\)\|", trimmed)]

        def table_rule(value: str) -> bool:
            parts = cells(value)
            return len(parts) >= 2 and all(re.fullmatch(r":?-{3,}:?", part.replace(" ", "")) is not None for part in parts)

        class SafeTableParser(HTMLParser):
            """Extract only semantic table structure and plain cell text."""

            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.table_depth = 0
                self.section = "body"
                self.row: list[tuple[str, str, str]] | None = None
                self.cell_tag = ""
                self.cell_scope = ""
                self.cell_text: list[str] = []
                self.rows: list[tuple[str, list[tuple[str, str, str]]]] = []
                self.ignored_depth = 0

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                name = tag.casefold()
                if name == "table": self.table_depth += 1; return
                if self.table_depth != 1: return
                if name in {"script", "style", "template", "iframe", "object", "embed", "svg", "math"}:
                    self.ignored_depth += 1; return
                if self.ignored_depth: return
                if name in {"thead", "tbody", "tfoot"}: self.section = "head" if name == "thead" else "body"; return
                if name == "tr": self.row = []; return
                if name in {"th", "td"} and self.row is not None:
                    self.cell_tag = name
                    scope = dict(attrs).get("scope") or ""
                    self.cell_scope = scope if name == "th" and scope in {"col", "row"} else ""
                    self.cell_text = []
                elif name == "br" and self.cell_tag:
                    self.cell_text.append(" ")

            def handle_data(self, data: str) -> None:
                if self.table_depth == 1 and not self.ignored_depth and self.cell_tag: self.cell_text.append(data)

            def handle_endtag(self, tag: str) -> None:
                name = tag.casefold()
                if self.table_depth == 1 and self.ignored_depth:
                    if name in {"script", "style", "template", "iframe", "object", "embed", "svg", "math"}: self.ignored_depth -= 1
                    return
                if self.table_depth == 1 and name in {"th", "td"} and self.cell_tag == name and self.row is not None:
                    text_value = " ".join("".join(self.cell_text).split())
                    self.row.append((self.cell_tag, self.cell_scope, text_value))
                    self.cell_tag = ""; self.cell_scope = ""; self.cell_text = []
                elif self.table_depth == 1 and name == "tr" and self.row is not None:
                    if self.row: self.rows.append((self.section, self.row))
                    self.row = None
                elif name == "table" and self.table_depth:
                    self.table_depth -= 1

        def html_table(block: str) -> str | None:
            parser = SafeTableParser()
            try:
                parser.feed(block); parser.close()
            except (ValueError, AssertionError):
                return None
            if not parser.rows: return None
            head_rows = [row for section, row in parser.rows if section == "head"]
            body_rows = [row for section, row in parser.rows if section != "head"]

            def render_rows(rows: list[list[tuple[str, str, str]]]) -> str:
                rendered: list[str] = []
                for row in rows:
                    rendered_cells = []
                    for tag, scope, text_value in row:
                        scope_attr = f' scope="{scope}"' if scope else ""
                        rendered_cells.append(f"<{tag}{scope_attr}>{inline(text_value)}</{tag}>")
                    rendered.append("<tr>" + "".join(rendered_cells) + "</tr>")
                return "".join(rendered)

            head_html = f"<thead>{render_rows(head_rows)}</thead>" if head_rows else ""
            body_html = f"<tbody>{render_rows(body_rows)}</tbody>"
            return f'<figure class="wp-block-table seo-control-table-wrap" style="display:block;width:100%;max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;"><table class="seo-control-table" style="width:100%;min-width:640px;border-collapse:collapse;">{head_html}{body_html}</table></figure>'

        def list_item(value: str) -> str:
            task = re.match(r"^\[([ xX])\]\s+(.+)$", value)
            if task is None: return f"<li>{inline(value)}</li>"
            checked = " checked" if task.group(1).casefold() == "x" else ""
            state = "Completed" if checked else "Pending"
            return f'<li class="task-list-item"><input type="checkbox" disabled{checked} aria-label="{state} checklist item"><span>{inline(task.group(2))}</span></li>'

        index = 0
        while index < len(lines):
            value = lines[index].strip()
            if not value:
                index += 1
                continue
            heading = re.match(r"^(#{1,3})\s+(.+)$", value)
            if heading:
                level = len(heading.group(1)); output.append(f"<h{level}>{inline(heading.group(2).strip())}</h{level}>"); index += 1; continue
            if re.match(r"^<table(?:\s|>)", value, flags=re.IGNORECASE):
                block: list[str] = []
                while index < len(lines):
                    block.append(lines[index])
                    closed = re.search(r"</table\s*>", lines[index], flags=re.IGNORECASE) is not None
                    index += 1
                    if closed: break
                rendered_table = html_table("\n".join(block))
                output.append(rendered_table if rendered_table is not None else f"<p>{inline(' '.join(block))}</p>")
                continue
            if "|" in value and index + 1 < len(lines) and table_rule(lines[index + 1]):
                header = cells(value); index += 2; body: list[list[str]] = []
                while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                    row = cells(lines[index])
                    if len(row) == len(header): body.append(row)
                    index += 1
                head_html = "".join(f"<th>{inline(cell)}</th>" for cell in header)
                body_html = "".join("<tr>" + "".join(f"<td>{inline(cell)}</td>" for cell in row) + "</tr>" for row in body)
                output.append(f'<figure class="wp-block-table seo-control-table-wrap" style="display:block;width:100%;max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;"><table class="seo-control-table" style="width:100%;min-width:640px;border-collapse:collapse;"><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table></figure>')
                continue
            unordered = re.match(r"^(?:[-*])\s+(.+)$", value)
            ordered = re.match(r"^\d+[.)]\s+(.+)$", value)
            if unordered or ordered:
                tag = "ul" if unordered else "ol"; items: list[str] = []
                pattern = r"^(?:[-*])\s+(.+)$" if unordered else r"^\d+[.)]\s+(.+)$"
                first_number = int(re.match(r"^(\d+)", value).group(1)) if ordered else 1
                while index < len(lines):
                    item = re.match(pattern, lines[index].strip())
                    if item is None: break
                    item_parts = [item.group(1)]; index += 1
                    while index < len(lines):
                        continuation = lines[index].strip()
                        if not continuation or re.match(r"^(#{1,3})\s+", continuation) or re.match(r"^(?:[-*])\s+", continuation) or re.match(r"^\d+[.)]\s+", continuation) or re.match(r"^<table(?:\s|>)", continuation, flags=re.IGNORECASE) or ("|" in continuation and index + 1 < len(lines) and table_rule(lines[index + 1])): break
                        item_parts.append(continuation); index += 1
                    items.append(list_item(" ".join(item_parts)))
                    next_index = index
                    while next_index < len(lines) and not lines[next_index].strip(): next_index += 1
                    if next_index < len(lines) and re.match(pattern, lines[next_index].strip()): index = next_index; continue
                    index = next_index; break
                list_class = ' class="task-list"' if tag == "ul" and any('class="task-list-item"' in item for item in items) else ""
                start = f' start="{first_number}"' if tag == "ol" and first_number != 1 else ""
                output.append(f"<{tag}{list_class}{start}>" + "".join(items) + f"</{tag}>")
                continue
            paragraph = [value]; index += 1
            while index < len(lines):
                next_value = lines[index].strip()
                if not next_value or re.match(r"^(#{1,3})\s+", next_value) or re.match(r"^(?:[-*])\s+", next_value) or re.match(r"^\d+[.)]\s+", next_value) or re.match(r"^<table(?:\s|>)", next_value, flags=re.IGNORECASE) or ("|" in next_value and index + 1 < len(lines) and table_rule(lines[index + 1])):
                    break
                paragraph.append(next_value); index += 1
            output.append(f"<p>{inline(' '.join(paragraph))}</p>")
        return "\n".join(output)

    @staticmethod
    def _authority_source_type(domain: str) -> str:
        value = domain.casefold()
        if value.endswith(".gov") or ".gov." in value: return "government"
        if any(token in value for token in ("iec.ch", "iso.org", "astm.org", "nfpa.org", "ul.com", "intertek", "tuv")): return "standard" if any(token in value for token in ("iec.ch", "iso.org", "astm.org", "nfpa.org")) else "certification"
        return "industry_research"

    @staticmethod
    def _authority_source_payload(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row); value["tags"] = json.loads(value.pop("tags_json") or "[]"); value["classification"] = json.loads(value.pop("classification_json") or "{}"); return value

    def _delete_content_memory(self, memory_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None: return
        with self._database() as connection, connection:
            cursor = connection.execute("DELETE FROM competitor_content_memory WHERE id=? AND project_id=?", (memory_id, project_id))
        if cursor.rowcount != 1: self._json(HTTPStatus.NOT_FOUND, {"error": "content memory does not exist in this website."}); return
        self._json(HTTPStatus.OK, {"deleted": 1})

    def _generate_content(self, asset_id: int, action: str, payload: Mapping[str, Any]) -> None:
        """Run one or all evidence-grounded synthesis stages and persist every result."""
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        job_id: int | None = None
        provider = self._optional_text(payload, "provider") or "openai"
        model: str | None = None
        reviewer_provider: str | None = None
        reviewer_model: str | None = None
        research_generator: Any | None = None
        research_provider: str | None = None
        research_model: str | None = None
        routing_mode = "manual"
        routing_summary = "Manual route: the selected writer is used for every writing node."
        try:
            with self._database() as connection:
                asset = self._content_asset(connection, project_id, asset_id)
                if action in {"generate", "generate-outline", "generate-draft"}:
                    self._require_content_competitor_learning(connection, asset)
                generator, provider, model, research_generator, research_provider, research_model, reviewer_provider, reviewer_model, routing_mode, routing_summary = self._content_execution_route(payload)
                job_id = self._start_content_generation_job(
                    connection, asset, action, provider, model,
                    reviewer_provider=reviewer_provider, reviewer_model=reviewer_model,
                    routing_mode=routing_mode, routing_summary=routing_summary,
                )
                # Retrieve compact, project-scoped strategy cards before any
                # model call.  Raw competitor text is never sent through this
                # path: only reviewed summaries and evidence metadata are.
                payload = dict(payload)
                payload["learning_memories"] = self._select_content_learning_memories(connection, asset)
                if generator is None:
                    job = self._finish_content_generation_job(connection, job_id, status="failed", failed_stage="configuration", error_summary="Selected provider is not configured.")
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"{self._content_provider_label(provider)} configuration 未配置，未切换到其他模型。", "generation_job": job})
                    return
                result: dict[str, Any] = {}
                # The project console sends the current website's first-party
                # knowledge on every generation request.  Refresh only that
                # source family inside an existing Brief, so an old Brief can
                # never silently suppress newly collected product/company data.
                if asset["current_brief_id"] is not None:
                    self._refresh_current_brief_company_knowledge(connection, asset, payload)
                # A one-click run respects an existing user-approved Brief.  It only
                # creates a Brief when there is no fact boundary to carry forward.
                if action == "generate-brief" or (action == "generate" and (asset["current_brief_id"] is None or payload.get("competitor_research") is True)):
                    result["brief"] = self._generate_ai_brief(connection, asset, payload, generator, provider, model, job_id)
                    asset = self._content_asset(connection, project_id, asset_id)
                if action in {"generate-outline", "generate"}:
                    result["outline"] = self._generate_ai_outline(connection, asset, payload, generator, provider, model, job_id)
                    asset = self._content_asset(connection, project_id, asset_id)
                if action in {"generate-draft", "generate"}:
                    result["draft"] = self._generate_ai_draft(connection, asset, payload, generator, provider, model, job_id)
                    asset = self._content_asset(connection, project_id, asset_id)
                if action == "review-quality" or (action == "generate" and routing_mode == "auto_collaborate"):
                    reviewer_generator, active_reviewer_provider, active_reviewer_model = self._content_reviewer_generator(
                        writer_generator=generator,
                        writer_provider=provider,
                        writer_model=model,
                        reviewer_provider=reviewer_provider,
                        reviewer_model=reviewer_model,
                    )
                    if reviewer_generator is None:
                        raise ValueError(f"{self._content_provider_label(reviewer_provider or provider)} reviewer configuration is not available")
                    result["quality_review"] = self._generate_ai_quality_review(
                        connection, asset, reviewer_generator, active_reviewer_provider, active_reviewer_model, job_id,
                    )
                    result["draft"] = result["quality_review"]["draft"]
                if action == "rewrite-targeted":
                    result["draft"] = self._generate_ai_targeted_rewrite(connection, asset, generator, provider, model, job_id)
                result["generation_job"] = self._finish_content_generation_job(connection, job_id, status="completed")
                refreshed = self._content_asset_detail(connection, project_id, asset_id)
                result["runs"] = refreshed["generation_runs"]
                result["asset"] = refreshed
        except (ContentGenerationProtocolError, CompetitorContentProtocolError) as error:
            detail = str(error) or "AI content generation returned invalid JSON."
            stage = "competitor_research" if isinstance(error, CompetitorContentProtocolError) else self._content_failed_stage(detail)
            job = None
            if job_id is not None:
                with self._database() as connection:
                    job = self._finish_content_generation_job(connection, job_id, status="failed", failed_stage=stage, error_summary=detail)
            if stage == "competitor_research":
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": f"当前标题不适合进入 SEO 竞品学习：{detail}。请返回标题库重选或改写为更自然、更接近搜索需求的标题；本次不会继续生成缺少竞品依据的正文，旧版本已保留。", "generation_job": job})
                return
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
        return {"semantic": "语义分析", "title": "标题与元信息", "outline": "文章大纲", "section": "章节写作", "assembly": "组装全文", "full_article": "整篇文章写作", "configuration": "模型配置", "preparation": "任务准备"}.get(stage, stage)

    @staticmethod
    def _content_failed_stage(error: str) -> str:
        matched = re.search(r"AI content (industry_rules|semantic|title|outline|chapter_plan|section|assembly|full_article|qa)", error)
        return matched.group(1) if matched else "generation"

    @staticmethod
    def _start_content_generation_job(
        connection: sqlite3.Connection,
        asset: sqlite3.Row,
        action: str,
        provider: str,
        model: str | None,
        *,
        reviewer_provider: str | None,
        reviewer_model: str | None,
        routing_mode: str,
        routing_summary: str,
    ) -> int:
        with connection:
            cursor = connection.execute(
                """INSERT INTO content_generation_jobs(
                       project_id,content_asset_id,requested_action,provider,model,reviewer_provider,reviewer_model,
                       routing_mode,routing_summary
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (asset["project_id"], asset["id"], action, provider, model, reviewer_provider, reviewer_model, routing_mode, routing_summary),
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
        requested_model = self._optional_text(payload, "model")
        if requested_model is not None and not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", requested_model):
            raise ValueError("model contains unsupported characters")
        injected = getattr(self.server, "content_generator", None)
        if injected is not None:
            # Test and local injected generators advertise the model they
            # actually run.  A UI request must not forge their audit record.
            return injected, requested or str(getattr(injected, "provider", "custom")), getattr(injected, "model", None)
        provider = requested or _ai_assignments(self.server.ai_settings_path).get("content_generation", "openai")
        if not getattr(self.server, "allow_environment_ai_fallback", True) and not _ai_settings_document(self.server.ai_settings_path).get("providers"):
            return None, provider, None
        configuration = _provider_configuration(self.server.ai_settings_path, provider)
        if configuration is None:
            return None, provider, None
        api_key, base_url, configured_model = configuration
        model = requested_model or configured_model
        # Full-article assembly receives several independently drafted H2
        # chapters.  It is intentionally allowed longer than the small
        # keyword/title calls, without changing the selected provider or
        # falling back to another model.
        return OpenAICompatibleContentGenerator(api_key, base_url, model, provider=provider, timeout=240.0), provider, model

    def _content_execution_route(
        self, payload: Mapping[str, Any]
    ) -> tuple[Any | None, str, str | None, Any | None, str | None, str | None, str | None, str | None, str, str]:
        """Resolve a task's immutable route before its first model call.

        Automatic collaboration deliberately has no hidden fallback: it needs
        both configured providers.  DeepSeek handles source/competitor
        interpretation and independent QA; ChatGPT handles the user-facing
        brief, outline, H2 chapters and final assembly.  Each concrete stage
        still writes its provider/model into ``content_generation_runs``.
        """
        mode = self._optional_text(payload, "routing_mode") or "manual"
        if mode not in {"manual", "auto_collaborate"}:
            raise ValueError("routing_mode must be manual or auto_collaborate")
        if mode == "manual":
            writer, writer_provider, writer_model = self._content_generator(payload)
            reviewer_provider, reviewer_model = self._content_reviewer_selection(payload, writer_provider, writer_model)
            return (
                writer, writer_provider, writer_model,
                None, None, None,
                reviewer_provider, reviewer_model,
                mode, "Manual route: the selected writer is used for every writing node.",
            )

        writer_request: dict[str, Any] = {"provider": "openai"}
        requested_writer_model = self._optional_text(payload, "model")
        if requested_writer_model:
            writer_request["model"] = requested_writer_model
        writer, writer_provider, writer_model = self._content_generator(writer_request)
        researcher, researcher_provider, researcher_model = self._content_generator({"provider": "deepseek"})
        if writer is None or researcher is None:
            missing = []
            if writer is None:
                missing.append("ChatGPT")
            if researcher is None:
                missing.append("DeepSeek")
            raise ValueError(f"自动协作需要先在 AI 与集成中配置 {' 和 '.join(missing)}；系统不会静默改用单一模型。")

        requested_reviewer = self._optional_text(payload, "reviewer_provider")
        if requested_reviewer is None:
            reviewer_provider, reviewer_model = researcher_provider, researcher_model
            reviewer_note = "DeepSeek 独立审核"
        else:
            reviewer_provider, reviewer_model = self._content_reviewer_selection(payload, writer_provider, writer_model)
            reviewer_note = f"人工指定 {self._content_provider_label(reviewer_provider)} 审核"
        summary = (
            f"自动协作：DeepSeek（{researcher_model or '默认模型'}）负责竞品/来源分析与 {reviewer_note}；"
            f"ChatGPT（{writer_model or '默认模型'}）负责 Brief、大纲、H2 写作和全文组装。"
        )
        return (
            writer, writer_provider, writer_model,
            researcher, researcher_provider, researcher_model,
            reviewer_provider, reviewer_model,
            mode, summary,
        )

    def _content_reviewer_selection(
        self,
        payload: Mapping[str, Any],
        writer_provider: str,
        writer_model: str | None,
    ) -> tuple[str, str | None]:
        """Persist the requested review route before that node is introduced.

        The current synchronous workflow has no separate QA request yet.  We
        still record the user's selection at job creation, so a later QA node
        can use it without mutating an in-flight writer job or losing audit
        history.
        """
        requested = self._optional_text(payload, "reviewer_provider")
        if requested is not None and requested not in AI_PROVIDERS:
            raise ValueError("reviewer_provider must be openai, gemini, or deepseek")
        provider = requested or writer_provider
        requested_model = self._optional_text(payload, "reviewer_model")
        if requested_model is not None:
            if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", requested_model):
                raise ValueError("reviewer_model contains unsupported characters")
            return provider, requested_model
        if provider == writer_provider:
            return provider, writer_model
        configuration = _provider_configuration(self.server.ai_settings_path, provider)
        return provider, configuration[2] if configuration is not None else None

    def _content_reviewer_generator(
        self,
        *,
        writer_generator: Any,
        writer_provider: str,
        writer_model: str | None,
        reviewer_provider: str | None,
        reviewer_model: str | None,
    ) -> tuple[Any | None, str, str | None]:
        provider = reviewer_provider or writer_provider
        model = reviewer_model or writer_model
        if provider == writer_provider and model == writer_model:
            return writer_generator, provider, model
        # Local/test injected generators advertise their physical model; do
        # not fabricate an audit identity merely because a route was picked.
        if getattr(self.server, "content_generator", None) is not None:
            return writer_generator, writer_provider, writer_model
        configuration = _provider_configuration(self.server.ai_settings_path, provider)
        if configuration is None:
            return None, provider, model
        api_key, base_url, configured_model = configuration
        active_model = model or configured_model
        return OpenAICompatibleContentGenerator(api_key, base_url, active_model, provider=provider, timeout=240.0), provider, active_model

    def _generate_ai_quality_review(
        self,
        connection: sqlite3.Connection,
        asset: sqlite3.Row,
        generator: Any,
        provider: str,
        model: str | None,
        generation_job_id: int,
    ) -> dict[str, Any]:
        if asset["current_draft_id"] is None:
            raise ValueError("a current draft is required before quality review")
        draft = connection.execute("SELECT * FROM content_drafts WHERE id=? AND project_id=? AND content_asset_id=?", (asset["current_draft_id"], asset["project_id"], asset["id"])).fetchone()
        if draft is None:
            raise ValueError("current draft does not exist")
        brief = self._current_content_brief(connection, asset)
        brief_payload = self._ensure_brief_industry_policy(
            connection, asset, brief, self._content_brief_payload(brief), generator, provider, model, generation_job_id
        )
        outline = connection.execute("SELECT * FROM content_outlines WHERE id=?", (asset["current_outline_id"],)).fetchone() if asset["current_outline_id"] else None
        review_data = {
            "canonical_title": asset["title_snapshot"], "primary_keyword": asset["keyword"],
            "project_context": brief_payload["brief"].get("project_context", {}),
            "writing_policy": self._writing_policy(brief_payload["brief"]),
            "article": {"title": draft["title"], "markdown": self._sanitize_reader_markdown(str(draft["markdown"])), "meta_description": draft["meta_description"]},
            "outline": self._content_outline_payload(connection, outline)["sections"] if outline else [],
            "sources": brief_payload["sources"], "learning_memories": brief_payload["brief"].get("learning_memories", []),
            "rules": {"max_targeted_rewrites": 2, "h2_guidance": "natural, normally around 5-7 but never forced", "no_unrelated_references": True, "tables_must_be_real_html_on_publish": True, "image_requirement": "800x600 WebP when images are selected"},
        }
        review, _run = self._run_content_stage(connection, asset, "qa", review_data, generator, provider, model, generation_job_id)
        review = dict(review)
        markdown = str(draft["markdown"] or "")
        keyword = str(asset["keyword"] or "").strip()
        word_count = self._english_word_count(markdown)
        bold_keyword_count = self._bold_primary_keyword_count(markdown, keyword)
        target_words = sum(
            max(0, int(section.get("target_words") or section.get("word_budget") or 0))
            for section in review_data["outline"]
            if isinstance(section, Mapping)
        )
        approved_max_words = 0
        if not target_words:
            outline_run = connection.execute(
                """SELECT output_json FROM content_generation_runs
                   WHERE content_asset_id=? AND stage='outline' AND status='completed'
                     AND output_json LIKE ?
                   ORDER BY id DESC LIMIT 1""",
                (asset["id"], '%"recommended_word_range"%'),
            ).fetchone()
            if outline_run is not None:
                raw_outline_output = outline_run["output_json"]
                if isinstance(raw_outline_output, Mapping):
                    outline_output = dict(raw_outline_output)
                else:
                    try:
                        outline_output = json.loads(str(raw_outline_output or "{}"))
                    except (TypeError, json.JSONDecodeError):
                        outline_output = {}
                if isinstance(outline_output, Mapping):
                    generated_sections = outline_output.get("sections")
                    if isinstance(generated_sections, list):
                        target_words = sum(
                            max(0, int(section.get("target_words") or 0))
                            for section in generated_sections
                            if isinstance(section, Mapping)
                        )
                    recommended_range = outline_output.get("recommended_word_range")
                    if isinstance(recommended_range, Mapping):
                        approved_max_words = max(0, int(recommended_range.get("max") or 0))
        allowed_words = approved_max_words or (int(round(target_words * 1.2)) if target_words else 0)
        blockers = [str(item) for item in review.get("critical_blockers", []) if isinstance(item, str) and item.strip()]
        raw_rewrites = review.get("targeted_rewrite")
        rewrites = [dict(item) for item in raw_rewrites if isinstance(item, Mapping)][:2] if isinstance(raw_rewrites, list) else []
        deterministic_rewrites: list[dict[str, str]] = []
        checks = [dict(item) for item in review.get("checks", []) if isinstance(item, Mapping)] if isinstance(review.get("checks"), list) else []
        if bold_keyword_count != 1:
            blockers.append(f"The exact primary keyword is bolded {bold_keyword_count} times; exactly one body occurrence is required.")
            checks.append({"name": "Deterministic primary-keyword emphasis", "status": "fail", "note": f"Found {bold_keyword_count} bold occurrences; required exactly 1."})
            deterministic_rewrites.append({
                "target": "Whole article formatting",
                "issue": f"The exact primary keyword is bolded {bold_keyword_count} times.",
                "instruction": "Keep exactly one bold exact-match primary keyword in the opening body paragraph and remove bold emphasis from every later exact-match occurrence; do not alter headings or evidence markers.",
            })
        else:
            checks.append({"name": "Deterministic primary-keyword emphasis", "status": "pass", "note": "Exactly one bold body occurrence was found."})
        if allowed_words and word_count > allowed_words:
            blockers.append(f"The article has {word_count} words, above the approved proportional ceiling of {allowed_words} words.")
            checks.append({"name": "Deterministic article length", "status": "fail", "note": f"Found {word_count} words; proportional ceiling is {allowed_words}."})
            deterministic_rewrites.append({
                "target": "Whole article",
                "issue": f"The draft is {word_count} words, above the {allowed_words}-word proportional ceiling.",
                "instruction": f"Shorten the complete article to no more than {allowed_words} words by removing repetition and overly granular checklist prose while preserving all six H2 roles, supported facts, and useful decision boundaries. Do not introduce reader-facing verification labels.",
            })
        elif allowed_words:
            checks.append({"name": "Deterministic article length", "status": "pass", "note": f"Found {word_count} words; proportional ceiling is {allowed_words}."})
        review["checks"] = checks
        review["critical_blockers"] = list(dict.fromkeys(blockers))
        review["targeted_rewrite"] = [*deterministic_rewrites, *rewrites][:2]
        qa_status = str(review.get("status") or "needs_verification")
        if qa_status not in {"approved", "needs_revision", "needs_verification"}:
            qa_status = "needs_verification"
        review["reviewer"] = {"provider": provider, "model": model}
        unresolved = [str(item) for item in review.get("unresolved_verify", []) if isinstance(item, str) and item.strip()] if isinstance(review.get("unresolved_verify"), list) else []
        try:
            draft_unresolved = json.loads(str(draft["unresolved_verify_json"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            draft_unresolved = []
        if not unresolved and isinstance(draft_unresolved, list):
            unresolved.extend(str(item) for item in draft_unresolved if isinstance(item, str) and item.strip())
        unresolved = list(dict.fromkeys(unresolved))
        review["unresolved_verify"] = unresolved
        if unresolved and review["targeted_rewrite"]:
            editable_rewrites: list[dict[str, Any]] = []
            for item in review["targeted_rewrite"]:
                rewrite_text = f"{item.get('issue', '')} {item.get('instruction', '')}".casefold()
                explicit_evidence_request = any(term in rewrite_text for term in ("supply", "provide", "add a source"))
                verification_dependency = "verify" in rewrite_text and any(
                    term in rewrite_text for term in ("manufacturer", "datasheet", "manual", "documentation", "specification", "specific controller", "source")
                )
                if not (explicit_evidence_request or verification_dependency):
                    editable_rewrites.append(item)
            review["targeted_rewrite"] = editable_rewrites
        if review["targeted_rewrite"]:
            qa_status = "needs_revision"
        elif unresolved:
            qa_status = "needs_verification"
        score = review.get("overall_score")
        if not isinstance(score, (int, float)):
            score = 0
        review["overall_score"] = max(0, min(int(score), 79 if (review["critical_blockers"] or unresolved) else 100))
        review["status"] = qa_status
        with connection:
            connection.execute("UPDATE content_drafts SET qa_json=?,qa_status=?,unresolved_verify_json=? WHERE id=?", (json.dumps(review, ensure_ascii=False), qa_status, json.dumps(unresolved, ensure_ascii=False), draft["id"]))
            authority_count = int(connection.execute("SELECT COUNT(*) FROM content_authority_source_links WHERE project_id=? AND content_asset_id=?", (asset["project_id"], asset["id"])).fetchone()[0])
            asset_status = "ready_to_publish" if qa_status == "approved" and authority_count else "needs_revision"
            connection.execute("UPDATE content_assets SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (asset_status, asset["id"]))
            updated = connection.execute("SELECT * FROM content_drafts WHERE id=?", (draft["id"],)).fetchone()
        return {"draft": self._content_draft_payload(updated), "review": review}

    def _generate_ai_targeted_rewrite(
        self,
        connection: sqlite3.Connection,
        asset: sqlite3.Row,
        generator: Any,
        provider: str,
        model: str | None,
        generation_job_id: int,
    ) -> dict[str, Any]:
        if asset["current_draft_id"] is None:
            raise ValueError("a current draft is required before targeted rewrite")
        draft = connection.execute("SELECT * FROM content_drafts WHERE id=? AND project_id=? AND content_asset_id=?", (asset["current_draft_id"], asset["project_id"], asset["id"])).fetchone()
        if draft is None:
            raise ValueError("current draft does not exist")
        qa = json.loads(str(draft["qa_json"] or "{}"))
        raw_instructions = qa.get("targeted_rewrite") if isinstance(qa, Mapping) else None
        instructions = [dict(item) for item in raw_instructions if isinstance(item, Mapping)][:2] if isinstance(raw_instructions, list) else []
        if not instructions:
            raise ValueError("quality review has no targeted rewrite instructions")
        rewrite_count = int(connection.execute(
            """SELECT COUNT(*) FROM content_drafts
               WHERE content_asset_id=? AND generation_job_id=?
                 AND qa_json LIKE ?""",
            (asset["id"], generation_job_id, "%rewrite_from_draft_id%"),
        ).fetchone()[0])
        if rewrite_count >= 2:
            raise ValueError("targeted rewrite limit reached; review the remaining issues manually")
        brief = self._current_content_brief(connection, asset)
        brief_payload = self._ensure_brief_industry_policy(
            connection, asset, brief, self._content_brief_payload(brief), generator, provider, model, generation_job_id
        )
        rewrite_data = {
            "canonical_title": asset["title_snapshot"], "primary_keyword": asset["keyword"],
            "project_context": brief_payload["brief"].get("project_context", {}),
            "writing_policy": self._writing_policy(brief_payload["brief"]),
            "article": {"title": draft["title"], "markdown": self._sanitize_reader_markdown(str(draft["markdown"])), "meta_description": draft["meta_description"]},
            "instructions": instructions, "sources": brief_payload["sources"],
            "learning_memories": brief_payload["brief"].get("learning_memories", []),
        }
        rewritten, run = self._run_content_stage(connection, asset, "targeted_rewrite", rewrite_data, generator, provider, model, generation_job_id)
        markdown = rewritten.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ContentGenerationProtocolError("AI content targeted_rewrite returned no Markdown.")
        verification = rewritten.get("verify") if isinstance(rewritten.get("verify"), list) else []
        rewrite_qa = {"status": "not_run", "rewrite_from_draft_id": draft["id"], "applied_targets": rewritten.get("applied_targets", []), "checks": [], "unresolved_verify": verification}
        with connection:
            version = int(connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM content_drafts WHERE content_asset_id=?", (asset["id"],)).fetchone()[0])
            sanitized_markdown = self._normalise_primary_keyword_emphasis(self._sanitize_reader_markdown(markdown), str(asset["keyword"] or ""))
            cursor = connection.execute("""INSERT INTO content_drafts(
                   project_id,content_asset_id,outline_id,generation_run_id,generation_job_id,parent_draft_id,
                   version,title,meta_description,markdown,sources_used_json,unresolved_verify_json,qa_json,
                   qa_status,provider,model
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (asset["project_id"], asset["id"], asset["current_outline_id"], run["id"], generation_job_id, draft["id"], version, draft["title"], str(rewritten.get("meta_description") or draft["meta_description"] or ""), sanitized_markdown, draft["sources_used_json"], json.dumps(verification, ensure_ascii=False), json.dumps(rewrite_qa, ensure_ascii=False), "not_run", provider, model))
            connection.execute("UPDATE content_assets SET status='needs_revision',current_draft_id=?,current_generation_run_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (cursor.lastrowid, run["id"], asset["id"]))
            updated = connection.execute("SELECT * FROM content_drafts WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._content_draft_payload(updated)

    @staticmethod
    def _normalise_industry_rules(value: Mapping[str, Any], explicit_industry: str) -> dict[str, Any]:
        """Keep the model policy bounded and make an explicit project industry authoritative."""
        list_fields = (
            "tone_rules", "structure_rules", "terminology_rules", "content_patterns",
            "prohibited_claims", "conversion_rules", "localization_rules",
        )
        rules: dict[str, Any] = {
            "industry": str(value.get("industry") or "").strip(),
            "industry_basis": str(value.get("industry_basis") or "unknown").strip().lower(),
            "audience_language": str(value.get("audience_language") or "").strip(),
        }
        try:
            confidence = float(value.get("industry_confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        rules["industry_confidence"] = max(0.0, min(confidence, 1.0))
        for field in list_fields:
            raw = value.get(field)
            rules[field] = list(dict.fromkeys(
                str(item).strip() for item in raw
                if isinstance(item, str) and item.strip()
            ))[:20] if isinstance(raw, list) else []
        raw_evidence = value.get("evidence_policy")
        evidence = dict(raw_evidence) if isinstance(raw_evidence, Mapping) else {}
        risk_level = str(evidence.get("risk_level") or "standard").strip().lower()
        if risk_level not in {"standard", "elevated", "ymyl"}:
            risk_level = "elevated"
        rules["evidence_policy"] = {
            "risk_level": risk_level,
            **{
                field: list(dict.fromkeys(
                    str(item).strip() for item in evidence.get(field, [])
                    if isinstance(item, str) and item.strip()
                ))[:20]
                for field in ("preferred_sources", "high_risk_claims", "required_disclosures")
            },
        }
        if explicit_industry:
            rules["industry"] = explicit_industry
            rules["industry_basis"] = "explicit"
            rules["industry_confidence"] = 1.0
        elif rules["industry_basis"] not in {"inferred", "unknown"}:
            rules["industry_basis"] = "inferred" if rules["industry"] else "unknown"
        return rules

    @staticmethod
    def _writing_policy(brief: Mapping[str, Any]) -> dict[str, Any]:
        industry_rules = brief.get("industry_rules")
        return {
            "fixed_safety_rules": list(FIXED_CONTENT_SAFETY_RULES),
            "industry_rules": dict(industry_rules) if isinstance(industry_rules, Mapping) else {},
            "priority": "Fixed safety and evidence rules override dynamic industry preferences.",
        }

    def _project_writing_context(self, connection: sqlite3.Connection, asset: sqlite3.Row) -> tuple[dict[str, Any], list[dict[str, str]]]:
        project = connection.execute(
            "SELECT id,name,site_url,industry,default_country,default_language FROM projects WHERE id=?",
            (asset["project_id"],),
        ).fetchone()
        if project is None:
            raise ValueError("project does not exist")
        project_context = {
            "project_id": int(project["id"]),
            "name": str(project["name"] or ""),
            "site_url": str(project["site_url"] or ""),
            "industry": str(project["industry"] or "").strip(),
            "country": str(project["default_country"] or ""),
            "language": str(project["default_language"] or ""),
        }
        rows = connection.execute(
            """SELECT title,source_type,url,knowledge_type,summary,content
               FROM project_knowledge_documents
               WHERE project_id=? AND status='ready'
               ORDER BY CASE WHEN summary<>'' THEN 0 ELSE 1 END,updated_at DESC,id DESC
               LIMIT 12""",
            (asset["project_id"],),
        ).fetchall()
        signals: list[dict[str, str]] = []
        for row in rows:
            summary = str(row["summary"] or "").strip()
            content = re.sub(r"\s+", " ", str(row["content"] or "")).strip()
            signals.append({
                "title": str(row["title"] or "")[:300],
                "source_type": str(row["source_type"] or ""),
                "knowledge_type": str(row["knowledge_type"] or ""),
                "url": str(row["url"] or "")[:1000],
                "summary_or_excerpt": (summary or content)[:700],
            })
        return project_context, signals

    def _generate_project_industry_rules(
        self,
        connection: sqlite3.Connection,
        asset: sqlite3.Row,
        generator: Any,
        provider: str,
        model: str | None,
        generation_job_id: int,
        *,
        audience: str,
        business_goal: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        project_context, knowledge_signals = self._project_writing_context(connection, asset)
        industry_input = {
            "project_context": project_context,
            "article_context": {
                "canonical_title": asset["title_snapshot"],
                "primary_keyword": asset["keyword"],
                "locale": asset["locale"],
                "target_audience": audience,
                "business_goal": business_goal,
            },
            "knowledge_signals": knowledge_signals,
            "fixed_safety_rules": list(FIXED_CONTENT_SAFETY_RULES),
            "instruction": "Generate rules only for this project and article. Do not reuse an industry policy from another project.",
        }
        raw_rules, _run = self._run_content_stage(
            connection, asset, "industry_rules", industry_input, generator, provider, model, generation_job_id
        )
        return project_context, self._normalise_industry_rules(raw_rules, project_context["industry"])

    def _ensure_brief_industry_policy(
        self,
        connection: sqlite3.Connection,
        asset: sqlite3.Row,
        brief: sqlite3.Row,
        brief_payload: dict[str, Any],
        generator: Any,
        provider: str,
        model: str | None,
        generation_job_id: int,
    ) -> dict[str, Any]:
        if isinstance(brief_payload["brief"].get("industry_rules"), Mapping):
            return brief_payload
        project_context, industry_rules = self._generate_project_industry_rules(
            connection, asset, generator, provider, model, generation_job_id,
            audience=str(brief["target_audience"] or ""),
            business_goal=str(brief["business_goal"] or ""),
        )
        updated = dict(brief_payload["brief"])
        updated["project_context"] = project_context
        updated["industry_rules"] = industry_rules
        updated["fixed_safety_rules"] = list(FIXED_CONTENT_SAFETY_RULES)
        with connection:
            connection.execute("UPDATE content_briefs SET brief_json=? WHERE id=?", (json.dumps(updated, ensure_ascii=False), brief["id"]))
        brief_payload["brief"] = updated
        return brief_payload

    def _generate_ai_brief(self, connection: sqlite3.Connection, asset: sqlite3.Row, payload: Mapping[str, Any], generator: Any, provider: str, model: str | None, generation_job_id: int, *, agent_job_id: int | None = None) -> dict[str, Any]:
        audience = self._optional_text(payload, "target_audience") or "US searchers evaluating this topic"
        goal = self._optional_text(payload, "business_goal") or "informational"
        manual_sources = self._content_sources(payload.get("sources", []))
        competitor_sources, analysis = self._research_sources(connection, asset["id"])
        authority_sources = self._authority_sources_for_asset(connection, asset)
        gsc_performance_sources = self._gsc_performance_sources_for_asset(connection, asset)
        self._link_authority_sources_for_asset(connection, asset, authority_sources)
        # Search Console data is deliberately a separate source family.  It
        # guides terminology, intent and internal-link choices, but is never
        # evidence for reader-facing product, safety, performance or ranking
        # claims.  Keeping the type explicit makes that boundary enforceable
        # in every model stage and auditable after generation.
        sources = authority_sources + gsc_performance_sources + competitor_sources + manual_sources
        project_context, industry_rules = self._generate_project_industry_rules(
            connection, asset, generator, provider, model, generation_job_id,
            audience=audience, business_goal=goal,
        )
        writing_policy = {
            "fixed_safety_rules": list(FIXED_CONTENT_SAFETY_RULES),
            "industry_rules": industry_rules,
            "priority": "Fixed safety and evidence rules override dynamic industry preferences.",
        }
        if analysis is None:
            data = {"topic": asset["title_snapshot"], "primary_keyword": asset["keyword"], "language_market": asset["locale"], "audience": audience, "business_goal": goal, "brand": self._optional_text(payload, "brand") or "", "project_context": project_context, "writing_policy": writing_policy, "sources": sources, "learning_memories": payload.get("learning_memories", []), "constraints": payload.get("constraints", [])}
            semantic, _run = self._run_content_stage(connection, asset, "semantic", data, generator, provider, model, generation_job_id)
        else:
            semantic = {"intent": {"dominant": analysis.get("search_intent", ""), "secondary": [], "reader_job": ""}, "entities": analysis.get("entities", []), "gaps_or_conflicts": [{"item": item, "action": "cover"} for item in analysis.get("missing_gaps", []) if isinstance(item, str)], "angle": "Competitor-informed original synthesis.", "must_cover": analysis.get("missing_gaps", [])}
        brief_json = {"project_context": project_context, "industry_rules": industry_rules, "fixed_safety_rules": list(FIXED_CONTENT_SAFETY_RULES), "semantic": semantic, "competitor_analysis": analysis or {}, "learning_memories": payload.get("learning_memories", []), "source_policy": "Material facts without a usable source must be recorded in unresolved_verify and omitted from reader-facing text, unless a genuinely general non-factual explanation is useful."}
        with connection:
            connection.execute("UPDATE content_briefs SET status='superseded' WHERE content_asset_id=? AND status='current'", (asset["id"],))
            cursor = connection.execute("INSERT INTO content_briefs(content_asset_id,target_audience,business_goal,target_length,sources_json,brief_json,agent_job_id) VALUES(?,?,?,?,?,?,?)", (asset["id"], audience, goal, 0, json.dumps(sources, ensure_ascii=False), json.dumps(brief_json, ensure_ascii=False), agent_job_id))
            connection.execute("UPDATE content_assets SET status='briefing',current_brief_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (cursor.lastrowid, asset["id"]))
            row = connection.execute("SELECT * FROM content_briefs WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._content_brief_payload(row)

    def _refresh_current_brief_company_knowledge(self, connection: sqlite3.Connection, asset: sqlite3.Row, payload: Mapping[str, Any]) -> None:
        """Refresh first-party knowledge and project-local GSC intelligence.

        A Brief is intentionally durable for manual research, but the website
        knowledge base and Search Console rows change independently.  The
        current generation payload is the scoped bridge from that website to
        this content project, so refresh those two source families while
        preserving manual, authority, and competitor sources already saved.
        """
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, list):
            return
        incoming = self._content_sources(raw_sources)
        company_sources = [source for source in incoming if source.get("source_type") == "company_knowledge"]
        brief = self._current_content_brief(connection, asset)
        existing = self._content_brief_payload(brief)["sources"]
        preserved = [
            source for source in existing
            if not (isinstance(source, Mapping) and source.get("source_type") == "gsc_performance")
        ]
        if company_sources:
            preserved = [
                source for source in preserved
                if not (isinstance(source, Mapping) and source.get("source_type") == "company_knowledge")
            ]
        refreshed_sources = preserved + company_sources + self._gsc_performance_sources_for_asset(connection, asset)
        with connection:
            connection.execute(
                "UPDATE content_briefs SET sources_json=? WHERE id=?",
                (json.dumps(refreshed_sources, ensure_ascii=False), brief["id"]),
            )

    @staticmethod
    def _gsc_context_terms(*values: Any) -> set[str]:
        ignored = {
            "about", "after", "also", "and", "are", "best", "can", "for", "from", "guide", "how", "into", "its",
            "led", "light", "lights", "more", "outdoor", "product", "products", "that", "the", "their", "this", "use",
            "what", "when", "which", "with", "your",
        }
        return {
            token.casefold()
            for value in values
            for token in re.findall(r"[A-Za-z0-9]{3,}", str(value or ""))
            if token.casefold() not in ignored
        }

    def _gsc_performance_sources_for_asset(self, connection: sqlite3.Connection, asset: sqlite3.Row) -> list[dict[str, Any]]:
        """Return small, topic-relevant Search Console planning signals.

        This is not an authority-source lookup.  The metrics remain private
        context for vocabulary, intent, overlap and internal-link decisions;
        writer prompts explicitly prohibit exposing them as reader evidence.
        """
        terms = self._gsc_context_terms(asset["keyword"], asset["title_snapshot"])
        rows = connection.execute(
            """SELECT id,query,page_url,clicks,impressions,ctr,position,collected_at
               FROM project_gsc_query_rows
               WHERE project_id=? AND TRIM(query)<>''
               ORDER BY impressions DESC, clicks DESC, id DESC
               LIMIT 120""",
            (asset["project_id"],),
        ).fetchall()
        ranked: list[tuple[int, float, sqlite3.Row]] = []
        for row in rows:
            query_terms = self._gsc_context_terms(row["query"])
            overlap = len(terms & query_terms)
            if terms and not overlap:
                continue
            ranked.append((overlap, float(row["impressions"] or 0), row))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        sources: list[dict[str, Any]] = []
        for _overlap, _impressions, row in ranked[:8]:
            query = str(row["query"] or "").strip()
            page_url = str(row["page_url"] or "").strip()
            if not query or not page_url:
                continue
            sources.append({
                "source_id": f"gsc-performance-{row['id']}",
                "source_type": "gsc_performance",
                "availability": "available",
                "title": f"Search Console intent signal: {query}",
                "url": page_url,
                "publisher": "Google Search Console (project-local planning data)",
                "collected_at": str(row["collected_at"] or ""),
                "content": (
                    "Private GSC planning signal. Use this only to understand reader wording, likely intent, "
                    "current-site overlap, or an internal-link opportunity. Never expose these metrics or use them "
                    "as reader-facing evidence.\n"
                    f"Observed query: {query}\nExisting site page: {page_url}\n"
                    f"Clicks: {float(row['clicks'] or 0):g}; impressions: {float(row['impressions'] or 0):g}; "
                    f"CTR: {float(row['ctr'] or 0):.4f}; average position: {float(row['position'] or 0):.1f}"
                ),
            })
        return sources

    def _authority_sources_for_asset(self, connection: sqlite3.Connection, asset: sqlite3.Row) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM authority_source_library WHERE project_id=? ORDER BY CASE authority_level WHEN 'primary' THEN 0 WHEN 'authoritative' THEN 1 WHEN 'supporting' THEN 2 ELSE 3 END,updated_at DESC", (asset["project_id"],)).fetchall()
        terms = {term.casefold() for term in re.findall(r"[A-Za-z0-9]+", f"{asset['keyword']} {asset['title_snapshot']}") if len(term) > 2}
        ranked = sorted(rows, key=lambda row: sum(term in f"{row['title']} {row['content']}".casefold() for term in terms), reverse=True)[:5]
        return [{"source_id": f"authority-{row['id']}", "source_type": "authority_source", "availability": "available", "url": row["url"], "publisher": row["publisher"], "published_at": row["published_at"], "title": row["title"], "authority_level": row["authority_level"], "content": row["content"][:12000]} for row in ranked]

    @staticmethod
    def _link_authority_sources_for_asset(connection: sqlite3.Connection, asset: sqlite3.Row, sources: list[dict[str, Any]]) -> None:
        source_ids = [int(str(source.get("source_id", "")).removeprefix("authority-")) for source in sources if str(source.get("source_id", "")).startswith("authority-") and str(source.get("source_id", "")).removeprefix("authority-").isdigit()]
        if not source_ids: return
        with connection:
            for source_id in source_ids:
                connection.execute("INSERT OR IGNORE INTO content_authority_source_links(project_id,content_asset_id,authority_source_id) VALUES(?,?,?)", (asset["project_id"], asset["id"], source_id))

    def _generate_ai_outline(self, connection: sqlite3.Connection, asset: sqlite3.Row, payload: Mapping[str, Any], generator: Any, provider: str, model: str | None, generation_job_id: int, *, agent_job_id: int | None = None) -> dict[str, Any]:
        brief = self._current_content_brief(connection, asset)
        brief_payload = self._ensure_brief_industry_policy(
            connection, asset, brief, self._content_brief_payload(brief), generator, provider, model, generation_job_id
        )
        semantic = brief_payload["brief"].get("semantic", {})
        competitor_analysis = brief_payload["brief"].get("competitor_analysis", {})
        writing_policy = self._writing_policy(brief_payload["brief"])
        project_context = brief_payload["brief"].get("project_context", {})
        if isinstance(competitor_analysis, Mapping) and isinstance(competitor_analysis.get("dynamic_outline"), list) and competitor_analysis["dynamic_outline"]:
            sections = competitor_analysis["dynamic_outline"]
            outline_json = {"intro_brief": "Answer the reader need directly.", "sections": sections, "conclusion_brief": "Summarize the decision and invite a B2B enquiry.", "cta_placement": "after conclusion"}
            metadata = {"selected_title": asset["title_snapshot"], "meta_description": ""}
            prepared_sections = sections
        else:
            prepared_sections = None
        learning_memories = brief_payload["brief"].get("learning_memories", [])
        title_data = {"semantic": semantic, "project_context": project_context, "writing_policy": writing_policy, "primary_keyword": asset["keyword"], "title_snapshot": asset["title_snapshot"], "voice": self._optional_text(payload, "voice") or "clear, helpful American English", "learning_memories": learning_memories, "year_rule": "none"}
        if prepared_sections is None:
            metadata, _run = self._run_content_stage(connection, asset, "title", title_data, generator, provider, model, generation_job_id)
            if not isinstance(metadata.get("selected_title"), str) or not metadata["selected_title"].strip():
                metadata["selected_title"] = asset["title_snapshot"]
            metadata["selected_title"] = asset["title_snapshot"]  # The user-approved title is canonical.
        updated_brief = self._content_brief_payload(brief)["brief"]
        updated_brief["metadata"] = metadata
        with connection:
            connection.execute("UPDATE content_briefs SET brief_json=? WHERE id=?", (json.dumps(updated_brief, ensure_ascii=False), brief["id"]))
        outline_data = {"semantic": semantic, "project_context": project_context, "writing_policy": writing_policy, "metadata": metadata, "cta": self._optional_text(payload, "cta") or "", "sources": brief_payload["sources"], "learning_memories": learning_memories}
        if prepared_sections is None:
            outline_json, _run = self._run_content_stage(connection, asset, "outline", outline_data, generator, provider, model, generation_job_id)
        else:
            company_sources = [source for source in brief_payload["sources"] if isinstance(source, Mapping) and source.get("source_type") == "company_knowledge"]
            if company_sources:
                plan_data = {
                    "topic": asset["title_snapshot"],
                    "primary_keyword": asset["keyword"],
                    "project_context": project_context,
                    "writing_policy": writing_policy,
                    "approved_outline": prepared_sections,
                    "sources": company_sources,
                    "instruction": "Plan company knowledge placement before H2 drafting; keep the approved competitor outline unchanged.",
                }
                company_plan, _run = self._run_content_stage(connection, asset, "company_context_plan", plan_data, generator, provider, model, generation_job_id)
                outline_json = {**outline_json, "company_context_plan": company_plan}
        sections = outline_json.get("sections")
        if not isinstance(sections, list) or not sections:
            raise ContentGenerationProtocolError("AI content outline returned no usable sections.")
        sections = self._deduplicate_ai_outline_sections(asset["title_snapshot"], sections)
        sections = self._apply_ai_company_context_plan(sections, outline_json.get("company_context_plan"), brief_payload["sources"])
        if not sections:
            raise ContentGenerationProtocolError("AI content outline contained only the canonical title or duplicate headings; no usable H2 sections remained.")
        outline_json = dict(outline_json)
        outline_json["sections"] = sections
        with connection:
            cursor = connection.execute("INSERT INTO content_outlines(content_asset_id,brief_id,status,agent_job_id) VALUES(?,?,?,?)", (asset["id"], brief["id"], "approved", agent_job_id))
            for position, section in enumerate(sections, 1):
                if not isinstance(section, Mapping) or not isinstance(section.get("heading"), str) or not section["heading"].strip():
                    raise ContentGenerationProtocolError("AI content outline has an invalid section.")
                section_data = self._normalise_outline_section(section, position)
                connection.execute("INSERT INTO content_outline_sections(outline_id,position,heading,purpose,word_budget,section_json) VALUES(?,?,?,?,?,?)", (cursor.lastrowid, position, section_data["heading"], section_data["purpose"], max(0, int(section_data.get("target_words") or 0)), json.dumps(section_data, ensure_ascii=False)))
            connection.execute("UPDATE content_assets SET status='outlining',current_outline_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (cursor.lastrowid, asset["id"]))
            row = connection.execute("SELECT * FROM content_outlines WHERE id=?", (cursor.lastrowid,)).fetchone()
        result = self._content_outline_payload(connection, row)
        result["blueprint"] = outline_json
        return result

    @staticmethod
    def _apply_ai_company_context_plan(sections: list[Mapping[str, Any]], plan: Any, sources: list[Any]) -> list[Mapping[str, Any]]:
        """Persist a bounded AI-planned company context assignment on its H2."""
        if not isinstance(plan, Mapping) or not isinstance(plan.get("assignments"), list):
            return sections
        company_sources = {
            str(source.get("source_id")): source
            for source in sources
            if isinstance(source, Mapping) and source.get("source_type") == "company_knowledge" and isinstance(source.get("source_id"), str)
        }
        by_heading = {KeywordDiscoveryRequestHandler._outline_heading_key(str(section.get("heading") or "")): dict(section) for section in sections}
        used_sources: set[str] = set()
        assigned = 0
        for item in plan["assignments"]:
            if assigned >= 2 or not isinstance(item, Mapping):
                break
            heading = item.get("section_heading")
            source_ids = item.get("source_ids")
            if not isinstance(heading, str) or not isinstance(source_ids, list):
                continue
            section = by_heading.get(KeywordDiscoveryRequestHandler._outline_heading_key(heading))
            if section is None:
                continue
            allowed = [source_id for source_id in source_ids if isinstance(source_id, str) and source_id in company_sources and source_id not in used_sources]
            if not allowed:
                continue
            source_id = allowed[0]
            source = company_sources[source_id]
            expected_url = str(source.get("url") or "")
            requested_url = item.get("link_url") if isinstance(item.get("link_url"), str) else ""
            section["company_context_source_ids"] = [source_id]
            section["company_context_role"] = str(item.get("factual_role") or "Relevant first-party product or brand context.").strip()
            if expected_url and requested_url == expected_url:
                section["company_context_link_url"] = expected_url
            used_sources.add(source_id)
            assigned += 1
        return [by_heading[KeywordDiscoveryRequestHandler._outline_heading_key(str(section.get("heading") or ""))] for section in sections]

    def _generate_ai_draft(self, connection: sqlite3.Connection, asset: sqlite3.Row, payload: Mapping[str, Any], generator: Any, provider: str, model: str | None, generation_job_id: int) -> dict[str, Any]:
        brief = self._current_content_brief(connection, asset)
        if asset["current_outline_id"] is None:
            raise ValueError("an AI outline is required before generating a draft")
        outline = connection.execute("SELECT * FROM content_outlines WHERE id=?", (asset["current_outline_id"],)).fetchone()
        if outline is None:
            raise ValueError("current content outline does not exist")
        brief_data = self._ensure_brief_industry_policy(
            connection, asset, brief, self._content_brief_payload(brief), generator, provider, model, generation_job_id
        )
        semantic = brief_data["brief"].get("semantic", {})
        competitor_learning = brief_data["brief"].get("competitor_analysis", {})
        learning_memories = brief_data["brief"].get("learning_memories", [])
        writing_policy = self._writing_policy(brief_data["brief"])
        project_context = brief_data["brief"].get("project_context", {})
        metadata = brief_data["brief"].get("metadata", {"selected_title": asset["title_snapshot"], "meta_description": ""})
        if not isinstance(metadata, Mapping): metadata = {"selected_title": asset["title_snapshot"], "meta_description": ""}
        outline_payload = self._content_outline_payload(connection, outline)
        blueprint = {"sections": outline_payload["sections"]}
        company_context = self._company_context_for_sections(
            sections=blueprint["sections"],
            sources=brief_data["sources"],
            article_text=f"{asset['title_snapshot']} {asset['keyword'] or ''}",
        )
        numbered_list_count = self._numbered_listicle_count(str(asset["title_snapshot"]))
        canonical_heading_key = self._outline_heading_key(str(asset["title_snapshot"]))
        full_article_sections: list[dict[str, Any]] = []
        allowed_link_urls: set[str] = set()
        selected_source_ids: set[str] = set()
        for section in blueprint["sections"]:
            assigned_company_sources = company_context.get(int(section["position"]), [])
            assigned_company_ids = {str(source.get("source_id")) for source in assigned_company_sources}
            section_context = {"id": f"s{section['position']}", **section}
            if numbered_list_count and self._outline_heading_key(str(section.get("heading") or "")) == canonical_heading_key:
                section_context["numbered_listicle_count"] = numbered_list_count
                section_context["numbered_listicle_instruction"] = f"Present exactly {numbered_list_count} distinct named ideas, each with a best-fit use case and a practical trade-off."
            if assigned_company_sources:
                section_context["company_context_source_ids"] = sorted(assigned_company_ids)
                section_context["company_context_instruction"] = "Use one relevant first-party product or company fact only when it helps this H2's reader decision."
            link_candidates = self._internal_link_candidates_for_section(
                project_site_url=str(project_context.get("site_url") or ""), section=section_context,
                sources=brief_data["sources"], assigned_company_source_ids=assigned_company_ids,
            )
            section_context["eligible_internal_links"] = link_candidates
            section_context["keyword_requirements"] = self._section_keyword_requirements(section_context, str(asset["keyword"] or ""))
            section_context["depth_requirements"] = self._section_depth_requirements(section_context)
            section_context["internal_link_budget"] = {"article_maximum": 3, "h2_maximum": 1}
            allowed_link_urls.update(candidate["target_url"] for candidate in link_candidates if candidate.get("target_url"))
            allowed_link_urls.update(self._canonical_internal_url(source.get("url")) for source in assigned_company_sources if self._canonical_internal_url(source.get("url")))
            selected_source_ids.update(str(source_id) for source_id in section_context.get("source_ids", []) if isinstance(source_id, str))
            selected_source_ids.update(assigned_company_ids)
            selected_source_ids.update(str(candidate["source_id"]) for candidate in link_candidates if candidate.get("source_id"))
            with connection:
                connection.execute("UPDATE content_outline_sections SET section_json=? WHERE outline_id=? AND position=?", (json.dumps(section_context, ensure_ascii=False), outline["id"], section["position"]))
            full_article_sections.append(section_context)
        scoped_sources = [source for source in brief_data["sources"] if isinstance(source, Mapping) and str(source.get("source_id") or "") in selected_source_ids]
        authority_urls = {
            self._canonical_internal_url(source.get("url")) for source in scoped_sources
            if source.get("source_type") == "authority_source" and self._canonical_internal_url(source.get("url"))
        }
        article_data = {
            "metadata": dict(metadata), "primary_keyword": str(asset["keyword"] or ""), "audience": brief["target_audience"],
            "intent": semantic.get("intent", {}), "project_context": project_context, "writing_policy": writing_policy,
            "angle": semantic.get("angle", ""), "competitor_learning": competitor_learning, "learning_memories": learning_memories,
            "outline": {"intro_brief": outline_payload.get("intro_brief", ""), "sections": full_article_sections, "conclusion_brief": outline_payload.get("conclusion_brief", "")},
            "overall_requirements": self._article_depth_requirements(full_article_sections),
            "sources": scoped_sources, "brand": self._optional_text(payload, "brand") or "", "cta": self._optional_text(payload, "cta") or "",
            "voice": self._optional_text(payload, "voice") or "clear, helpful American English", "language": asset["locale"],
            "reader_markdown_policy": "Return one complete Markdown article. Never display internal source IDs or verification labels; use portable Markdown tables and lists only.",
        }
        article, article_run = self._run_content_stage(connection, asset, "full_article", article_data, generator, provider, model, generation_job_id)
        if not isinstance(article.get("markdown"), str) or not article["markdown"].strip():
            raise ContentGenerationProtocolError("AI full article returned no Markdown.")
        markdown = self._sanitize_reader_markdown(article["markdown"])
        markdown = self._enforce_full_article_links(markdown, allowed_link_urls | authority_urls)
        markdown = self._normalise_primary_keyword_emphasis(markdown, str(asset["keyword"] or ""))
        verification = article.get("verify", []) if isinstance(article.get("verify", []), list) else []
        verification = list(dict.fromkeys(verification))
        sources_used = article.get("sources_used", []) if isinstance(article.get("sources_used", []), list) else []
        if not sources_used:
            sources_used = list(dict.fromkeys(
                source_id
                for claim in article.get("claims_used", []) if isinstance(claim, Mapping) and isinstance(claim.get("source_ids"), list)
                for source_id in claim["source_ids"] if isinstance(source_id, str)
            ))
        compatibility_qa = {"status": "not_run", "checks": [], "unresolved_verify": verification}
        content_tags = self._generate_content_tags(
            asset=asset,
            semantic=semantic,
            headings=[str(section.get("heading") or "") for section in blueprint["sections"]],
            markdown=markdown,
            generator=generator,
        )
        with connection:
            version = int(connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM content_drafts WHERE content_asset_id=?", (asset["id"],)).fetchone()[0])
            parent_draft_id = asset["current_draft_id"]
            cursor = connection.execute("""INSERT INTO content_drafts(
                   project_id,content_asset_id,outline_id,generation_run_id,generation_job_id,parent_draft_id,
                   version,title,meta_description,markdown,sources_used_json,unresolved_verify_json,qa_json,
                   qa_status,provider,model
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (asset["project_id"], asset["id"], outline["id"], article_run["id"], generation_job_id, parent_draft_id, version, str(article.get("title") or metadata.get("selected_title") or asset["title_snapshot"]), str(article.get("meta_description") or metadata.get("meta_description") or ""), markdown, json.dumps(sources_used, ensure_ascii=False), json.dumps(verification, ensure_ascii=False), json.dumps(compatibility_qa, ensure_ascii=False), "not_run", provider, model))
            source_count = int(connection.execute("SELECT COUNT(*) FROM content_authority_source_links WHERE project_id=? AND content_asset_id=?", (asset["project_id"], asset["id"])).fetchone()[0])
            asset_status = "ready_to_publish" if source_count else "needs_revision"
            connection.execute("UPDATE content_assets SET status=?,current_draft_id=?,current_generation_run_id=?,tags_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (asset_status, cursor.lastrowid, article_run["id"], json.dumps(content_tags, ensure_ascii=False), asset["id"]))
            draft = connection.execute("SELECT * FROM content_drafts WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._content_draft_payload(draft)

    @staticmethod
    def _section_keyword_requirements(section: Mapping[str, Any], primary_keyword: str) -> dict[str, Any]:
        """Give the full-article writer an H2-specific relevance floor, not a stuffing quota."""
        raw = section.get("keyword_requirements")
        supplied = raw.get("supporting_terms") if isinstance(raw, Mapping) else []
        terms = [re.sub(r"\s+", " ", str(item)).strip() for item in supplied if isinstance(item, str)] if isinstance(supplied, list) else []
        terms = [item[:90] for item in terms if 2 <= len(item) <= 90]
        if not terms:
            stop_words = {"about", "after", "also", "and", "are", "best", "can", "for", "from", "guide", "how", "into", "its", "that", "the", "their", "this", "use", "what", "when", "which", "with", "your"}
            seed = " ".join([str(section.get("heading") or ""), *[str(item) for item in section.get("key_points", []) if isinstance(item, str)]])
            terms = [token for token in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", seed) if token.casefold() not in stop_words][:4]
        terms = list(dict.fromkeys(terms))[:4]
        minimum = raw.get("minimum_supporting_terms") if isinstance(raw, Mapping) else None
        try:
            minimum = int(minimum)
        except (TypeError, ValueError):
            minimum = min(2, len(terms))
        if minimum <= 0:
            minimum = min(2, len(terms))
        return {
            "primary_keyword": primary_keyword,
            "primary_keyword_rule": "Use in the opening only; do not repeat it mechanically in every H2.",
            "supporting_terms": terms,
            "minimum_supporting_terms": max(0, min(minimum, len(terms))),
        }

    @staticmethod
    def _section_depth_requirements(section: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a measurable depth floor for every H2 before the single drafting call."""
        raw = section.get("depth_requirements")
        raw = raw if isinstance(raw, Mapping) else {}
        try:
            minimum_subtopics = int(raw.get("minimum_subtopics"))
        except (TypeError, ValueError):
            minimum_subtopics = max(3, min(5, len(section.get("key_points", [])) or 3))
        if minimum_subtopics <= 0:
            minimum_subtopics = max(3, min(5, len(section.get("key_points", [])) or 3))
        try:
            recommended_words = int(section.get("target_words") or 0)
        except (TypeError, ValueError):
            recommended_words = 0
        return {
            "minimum_non_overlapping_subtopics": max(2, min(minimum_subtopics, 5)),
            "minimum_words": max(180, min(420, recommended_words or 280)),
            "reader_outcome": str(raw.get("reader_outcome") or section.get("purpose") or "Help the reader make the next decision.")[:300],
            "required_practical_detail": str(raw.get("practical_detail") or section.get("reader_question") or "Give a practical check, condition, trade-off, or action.")[:300],
        }

    @staticmethod
    def _article_depth_requirements(sections: list[Mapping[str, Any]]) -> dict[str, Any]:
        section_minimums = [int((section.get("depth_requirements") or {}).get("minimum_words") or 0) for section in sections]
        return {
            "minimum_total_words": max(900, sum(section_minimums) + 140),
            "required_h2_count": len(sections),
            "depth_rule": "Every H2 must answer its distinct reader question and meet its own non-overlapping subtopic requirement; do not meet length by repeating introductions, conclusions, or generic cautions.",
            "keyword_rule": "Use the primary keyword once in the opening and use supporting terms only where they clarify the H2 topic. Natural relevance is more important than repetition.",
        }

    @classmethod
    def _enforce_full_article_links(cls, markdown: str, allowed_urls: set[str]) -> str:
        """Keep only exact, project-scoped or authority URLs supplied to the one-pass writer."""
        written_internal: set[str] = set()

        def keep_only_allowed(match: re.Match[str]) -> str:
            label = match.group(1)
            destination = cls._canonical_internal_url(match.group(2))
            if not destination or destination not in allowed_urls:
                return label
            is_internal = destination in allowed_urls
            if is_internal and destination in written_internal:
                return label
            if is_internal:
                if len(written_internal) >= 3:
                    return label
                written_internal.add(destination)
            return f"[{label}]({destination})"

        return re.sub(r"(?<!!)\[([^\]]+)\]\(([^\s)]+)(?:\s+['\"][^'\"]*['\"])?\)", keep_only_allowed, markdown)

    @staticmethod
    def _generate_content_tags(*, asset: sqlite3.Row, semantic: Any, headings: list[str], markdown: str, generator: Any) -> list[str]:
        """Keep tags deterministic so one draft uses exactly one writing prompt."""
        return KeywordDiscoveryRequestHandler._fallback_content_tags(asset, semantic, markdown)

    @staticmethod
    def _normalise_content_tags(raw_tags: Any) -> list[str]:
        if not isinstance(raw_tags, list):
            return []
        cleaned: list[str] = []
        for item in raw_tags:
            if not isinstance(item, str):
                continue
            tag = re.sub(r"[\r\n#]+", " ", item).strip(" -–—·，,。；;:：")
            if not (2 <= len(tag) <= 18) or tag.casefold() in {value.casefold() for value in cleaned}:
                continue
            cleaned.append(tag)
            if len(cleaned) == 3:
                break
        return cleaned

    @staticmethod
    def _fallback_content_tags(asset: sqlite3.Row, semantic: Any, markdown: str) -> list[str]:
        """Keep article filtering useful even if its optional tag call fails."""
        topic = str(asset["keyword"] or asset["title_snapshot"] or "").strip()
        stop_words = {"a", "an", "all", "and", "are", "for", "how", "of", "the", "to", "what", "with"}
        topic_tokens = [token for token in re.findall(r"[A-Za-z0-9]+", topic) if token.casefold() not in stop_words]
        if topic_tokens:
            topic_words: list[str] = []
            for token in topic_tokens:
                word = token.upper() if token.casefold() in {"led", "ip", "seo", "b2b"} else token.capitalize()
                candidate = " ".join([*topic_words, word])
                if len(candidate) > 18:
                    break
                topic_words.append(word)
                if len(topic_words) == 3:
                    break
            topic_tag = " ".join(topic_words) or "Article Topic"
        else:
            topic_tag = "Article Topic"
        topic_tag = topic_tag[:42]
        # Categorise from the approved topic/title rather than the full body:
        # a deep article often mentions installation, comparison and styles in
        # passing, which would otherwise produce a misleading primary tag.
        haystack = f"{asset['title_snapshot']} {topic}".casefold()
        intent = ""
        if isinstance(semantic, Mapping) and isinstance(semantic.get("intent"), Mapping):
            intent = str(semantic["intent"].get("dominant") or "").casefold()
        if any(marker in haystack for marker in ("waterproof", "ip rating", "ip-rated")):
            format_tag = "Waterproof Ratings"
        elif re.search(r"\b(?:vs\.?|versus|compare|comparison)\b", haystack):
            format_tag = "Product Comparison"
        elif any(marker in haystack for marker in ("idea", "inspiring", "design", "style")):
            format_tag = "Design Ideas"
        elif any(marker in haystack for marker in ("install", "installation", "how to")):
            format_tag = "Installation Guide"
        elif "step by step" in haystack:
            format_tag = "Verification Steps"
        elif any(marker in haystack for marker in ("troubleshoot", "problem", "mistake", "repair")):
            format_tag = "Troubleshooting"
        elif "commercial" in intent or "transactional" in intent:
            format_tag = "Buying Guide"
        else:
            format_tag = "Practical Guide"
        if any(marker in haystack for marker in ("outdoor", "solar", "garden", "deck", "landscape")):
            context_tag = "Outdoor Lighting"
        elif any(marker in haystack for marker in ("waterproof", "ip rating", "ip-rated")):
            context_tag = "Product Safety"
        elif "commercial" in intent or "transactional" in intent:
            context_tag = "Buying Decision"
        else:
            context_tag = "Industry Knowledge"
        return KeywordDiscoveryRequestHandler._normalise_content_tags([topic_tag, format_tag, context_tag])

    @staticmethod
    def _company_context_for_sections(*, sections: list[Mapping[str, Any]], sources: list[Any], article_text: str) -> dict[int, list[Mapping[str, Any]]]:
        """Select at most two genuinely related first-party sources per article.

        The model receives company knowledge only in the strongest matching
        H2s.  This gives the article a real brand/product footprint without
        repeating a sales paragraph throughout the article.
        """
        company_sources = [source for source in sources if isinstance(source, Mapping) and source.get("source_type") == "company_knowledge" and str(source.get("content") or "").strip()]
        if not sections or not company_sources:
            return {}
        by_id = {str(source.get("source_id")): source for source in company_sources if str(source.get("source_id") or "")}
        planned: dict[int, list[Mapping[str, Any]]] = {}
        used_planned: set[str] = set()
        for section in sections:
            if len(used_planned) >= 2:
                break
            planned_ids = section.get("company_context_source_ids", [])
            if not isinstance(planned_ids, list):
                continue
            for source_id in planned_ids:
                if not isinstance(source_id, str) or source_id in used_planned or source_id not in by_id:
                    continue
                planned[int(section["position"])] = [by_id[source_id]]
                used_planned.add(source_id)
                break
        if planned:
            return planned
        stop_words = {"about", "after", "also", "and", "are", "article", "best", "can", "for", "from", "guide", "into", "its", "led", "light", "lights", "more", "outdoor", "product", "products", "that", "the", "their", "this", "use", "what", "when", "which", "with", "your"}

        def terms(value: str) -> set[str]:
            return {token.casefold() for token in re.findall(r"[A-Za-z0-9]{3,}", value) if token.casefold() not in stop_words}

        article_terms = terms(article_text)
        choices: list[tuple[int, int, Mapping[str, Any]]] = []
        for source in company_sources:
            source_terms = terms(f"{source.get('title', '')} {str(source.get('content', ''))[:8000]}")
            article_match = len(source_terms & article_terms)
            # A first-party source with no subject overlap is not injected just
            # because it belongs to the site; this prevents unrelated products
            # from leaking into an article.
            if not article_match:
                continue
            for section in sections:
                section_text = " ".join(
                    [str(section.get("heading") or ""), str(section.get("purpose") or ""), str(section.get("reader_question") or ""), *[str(item) for item in section.get("key_points", []) if isinstance(item, str)]]
                )
                section_terms = terms(section_text)
                decision_bonus = 1 if section_terms & {"choose", "comparison", "compare", "installation", "maintenance", "selection", "specification", "supplier", "faq"} else 0
                score = article_match * 4 + len(source_terms & section_terms) * 5 + decision_bonus
                choices.append((score, int(section["position"]), source))
        assignments: dict[int, list[Mapping[str, Any]]] = {}
        used_source_ids: set[str] = set()
        for _score, position, source in sorted(choices, key=lambda item: item[0], reverse=True):
            source_id = str(source.get("source_id") or "")
            if not source_id or source_id in used_source_ids or len(used_source_ids) >= 2:
                continue
            if len(assignments.get(position, [])) >= 1:
                continue
            assignments.setdefault(position, []).append(source)
            used_source_ids.add(source_id)
        return assignments

    @staticmethod
    def _canonical_internal_url(value: Any) -> str:
        """Normalize one public URL for exact internal-link allow-list checks."""
        raw = str(value or "").strip()
        if not raw:
            return ""
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            return ""
        path = parsed.path or "/"
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))

    @staticmethod
    def _internal_host_key(value: str) -> str:
        """Treat www and apex forms of one project domain as the same site."""
        host = (urlsplit(value).hostname or "").casefold()
        return host[4:] if host.startswith("www.") else host

    @classmethod
    def _internal_link_candidates_for_section(
        cls,
        *,
        project_site_url: str,
        section: Mapping[str, Any],
        sources: list[Any],
        assigned_company_source_ids: set[str],
    ) -> list[dict[str, str]]:
        """Return only exact, project-local destinations the current H2 may use.

        GSC can point to an existing page but cannot prove a product fact.  A
        company page can provide product context only when it has already been
        assigned to this H2.  Keeping the candidate set small and explicit
        lets the model explain its choice without being able to invent routes.
        """
        project_host = cls._internal_host_key(project_site_url)
        if not project_host:
            return []
        section_terms = cls._gsc_context_terms(
            section.get("heading"), section.get("reader_question"), section.get("purpose"), section.get("key_points")
        )
        ranked: list[tuple[int, dict[str, str]]] = []
        seen_urls: set[str] = set()
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            source_id = str(source.get("source_id") or "")
            source_type = str(source.get("source_type") or "")
            url = cls._canonical_internal_url(source.get("url"))
            if not source_id or not url or cls._internal_host_key(url) != project_host or url in seen_urls:
                continue
            is_assigned_company_source = source_type == "company_knowledge" and source_id in assigned_company_source_ids
            is_gsc_source = source_type in {"gsc_performance", "gsc_anchor"}
            if not (is_assigned_company_source or is_gsc_source):
                continue
            source_terms = cls._gsc_context_terms(source.get("title"), source.get("content"))
            overlap = len(section_terms & source_terms)
            # Product context is deliberately scoped to the H2 chosen by the
            # company-context planner. GSC candidates remain optional and are
            # limited to the strongest matching existing-site pages.
            score = overlap * 10 + (3 if is_assigned_company_source else 0)
            ranked.append((score, {
                "source_id": source_id,
                "source_type": source_type,
                "target_url": url,
                "title": str(source.get("title") or "")[:300],
                "role": "assigned_product_context" if is_assigned_company_source else "gsc_internal_link_opportunity",
            }))
            seen_urls.add(url)
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [candidate for _score, candidate in ranked[:4]]

    @classmethod
    def _validated_product_recommendation(cls, raw_plan: Any, assigned_sources: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Keep product recommendations tied to the H2's assigned evidence."""
        empty = {"use": False, "source_id": "", "product_name": "", "supported_role": "", "link_url": "", "placement": "", "reason": ""}
        if not isinstance(raw_plan, Mapping) or raw_plan.get("use") is not True:
            return empty
        by_id = {str(source.get("source_id") or ""): source for source in assigned_sources}
        source_id = str(raw_plan.get("source_id") or "")
        source = by_id.get(source_id)
        if source is None:
            return empty
        expected_url = cls._canonical_internal_url(source.get("url"))
        requested_url = cls._canonical_internal_url(raw_plan.get("link_url"))
        if requested_url and requested_url != expected_url:
            return empty
        product_name = re.sub(r"\s+", " ", str(raw_plan.get("product_name") or "")).strip()[:160]
        supported_role = re.sub(r"\s+", " ", str(raw_plan.get("supported_role") or "")).strip()[:400]
        if not product_name or not supported_role:
            return empty
        return {
            "use": True,
            "source_id": source_id,
            "product_name": product_name,
            "supported_role": supported_role,
            "link_url": expected_url,
            "placement": re.sub(r"\s+", " ", str(raw_plan.get("placement") or "")).strip()[:240],
            "reason": re.sub(r"\s+", " ", str(raw_plan.get("reason") or "")).strip()[:400],
        }

    @classmethod
    def _validated_internal_link_plan(
        cls, raw_plan: Any, candidates: list[Mapping[str, str]], reserved_urls: set[str]
    ) -> dict[str, Any]:
        """Accept at most one link only when it exactly matches a supplied candidate."""
        empty = {"use": False, "anchor_text": "", "anchor_type": "", "target_url": "", "target_source_id": "", "placement": "", "reason": ""}
        if not isinstance(raw_plan, Mapping) or raw_plan.get("use") is not True:
            return empty
        source_id = str(raw_plan.get("target_source_id") or "")
        target_url = cls._canonical_internal_url(raw_plan.get("target_url"))
        candidate = next((item for item in candidates if item.get("source_id") == source_id and item.get("target_url") == target_url), None)
        if candidate is None or target_url in reserved_urls:
            return empty
        anchor_text = re.sub(r"\s+", " ", str(raw_plan.get("anchor_text") or "")).strip()
        # An anchor has to read like ordinary visible prose, not markup, a
        # URL, a call-to-action button, or an opaque keyword string.
        if not (2 <= len(anchor_text) <= 90) or re.search(r"[\[\]()<>]|https?://", anchor_text, flags=re.IGNORECASE):
            return empty
        anchor_type = str(raw_plan.get("anchor_type") or "")
        if anchor_type not in {"gsc_query", "related_term", "product_name", "descriptive"}:
            anchor_type = "descriptive"
        return {
            "use": True,
            "anchor_text": anchor_text,
            "anchor_type": anchor_type,
            "target_url": target_url,
            "target_source_id": source_id,
            "placement": re.sub(r"\s+", " ", str(raw_plan.get("placement") or "")).strip()[:240],
            "reason": re.sub(r"\s+", " ", str(raw_plan.get("reason") or "")).strip()[:400],
        }

    @classmethod
    def _enforce_section_internal_link_plan(cls, markdown: str, plan: Mapping[str, Any]) -> tuple[str, bool]:
        """Remove model-invented links while preserving their visible wording.

        The writer receives one validated plan. If it ignores that plan or
        produces a different route, it is safer to retain the sentence as
        plain text than to publish an unreviewed destination.
        """
        target_url = cls._canonical_internal_url(plan.get("target_url")) if plan.get("use") else ""
        link_written = False

        def keep_only_planned_link(match: re.Match[str]) -> str:
            nonlocal link_written
            label = match.group(1)
            destination = cls._canonical_internal_url(match.group(2))
            if target_url and destination == target_url and not link_written:
                link_written = True
                return f"[{label}]({target_url})"
            return label

        cleaned = re.sub(r"(?<!!)\[([^\]]+)\]\(([^\s)]+)(?:\s+['\"][^'\"]*['\"])?\)", keep_only_planned_link, markdown)
        return cleaned, link_written

    @staticmethod
    def _completed_generation_stage_output(
        connection: sqlite3.Connection, generation_job_id: int, stage: str, section_id: str,
    ) -> dict[str, Any] | None:
        """Reuse a completed chapter checkpoint when only a later H2 failed.

        Each content stage keeps its original input/output in the durable run
        table. A retry therefore resumes from the first missing H2 instead of
        paying for, or overwriting, chapters that have already succeeded.
        """
        stored_stage = "outline" if stage == "chapter_plan" else stage
        rows = connection.execute(
            """SELECT input_json,output_json FROM content_generation_runs
               WHERE generation_job_id=? AND stage=? AND status='completed'
               ORDER BY id DESC""",
            (generation_job_id, stored_stage),
        ).fetchall()
        for row in rows:
            try:
                stage_input = json.loads(row["input_json"] or "{}")
                output = json.loads(row["output_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(stage_input, Mapping) or not isinstance(output, Mapping):
                continue
            if stage == "chapter_plan" and stage_input.get("workflow_stage") != "chapter_plan":
                continue
            section = stage_input.get("current_section") if stage == "chapter_plan" else stage_input.get("section")
            if isinstance(section, Mapping) and section.get("id") == section_id:
                return dict(output)
        return None

    def _run_content_stage(self, connection: sqlite3.Connection, asset: sqlite3.Row, stage: str, data: Mapping[str, Any], generator: Any, provider: str, model: str | None, generation_job_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        # Existing local SQLite databases constrain the persisted stage column
        # to the original stage family. Preserve planning-only audit data in
        # input_json while storing it under that compatible outline family.
        # The API restores workflow_stage for the UI, so no detail is lost.
        stored_stage = "semantic" if stage == "industry_rules" else ("outline" if stage in {"chapter_plan", "company_context_plan"} else ("assembly" if stage == "full_article" else ("section" if stage == "targeted_rewrite" else stage)))
        logged_input = dict(data)
        if stage in {"industry_rules", "chapter_plan", "company_context_plan", "full_article", "targeted_rewrite"}:
            logged_input["workflow_stage"] = stage
        with connection:
            cursor = connection.execute("INSERT INTO content_generation_runs(project_id,content_asset_id,stage,provider,model,generation_job_id,status,input_json,prompt_version) VALUES(?,?,?,?,?,?,'running',?,?)", (asset["project_id"], asset["id"], stored_stage, provider, model, generation_job_id, json.dumps(logged_input, ensure_ascii=False), PROMPT_VERSION))
            run_id = int(cursor.lastrowid)
        value: dict[str, Any] | None = None
        last_error: Exception | None = None
        # A transient provider reset should not discard a whole article batch.
        # Completed earlier H2 checkpoints are reused on the next job retry;
        # this local retry handles short-lived failures before that is needed.
        for attempt in range(3):
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
                break
            except Exception as error:
                last_error = error
                if attempt < 2:
                    time.sleep(1.2 * (attempt + 1))
        if value is None:
            error = last_error or ContentGenerationProtocolError(f"AI content {stage} failed.")
            with connection:
                connection.execute("UPDATE content_generation_runs SET status='failed',error_summary=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (str(error) or f"AI content {stage} failed.", run_id))
            if isinstance(error, ContentGenerationProtocolError):
                raise
            detail = re.sub(r"(?i)(api[_-]?key|authorization|bearer|token)\s*[:=]?\s*[^\s,;]+", r"\1=[redacted]", str(error)).strip()
            raise ContentGenerationProtocolError(f"AI content {stage} request failed: {detail[:240] or type(error).__name__}.") from error
        with connection:
            connection.execute("UPDATE content_generation_runs SET status='completed',output_json=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(value, ensure_ascii=False), run_id))
        return value, {"id": run_id, "stage": stage, "status": "completed"}

    @staticmethod
    def _deduplicate_ai_outline_sections(canonical_title: str, sections: list[Any]) -> list[Mapping[str, Any]]:
        """Drop a model's accidental H1 echo and duplicate H2s before save.

        The selected title is always rendered as the sole H1 by assembly.  A
        repeated title from an otherwise valid AI outline is recoverable model
        formatting noise, not a reason to discard the entire content task.
        """
        canonical_key = KeywordDiscoveryRequestHandler._outline_heading_key(canonical_title)
        numbered_list_title = KeywordDiscoveryRequestHandler._numbered_listicle_count(canonical_title) is not None
        seen: set[str] = set()
        usable: list[Mapping[str, Any]] = []
        for section in sections:
            if not isinstance(section, Mapping):
                raise ContentGenerationProtocolError("AI content outline has an invalid section.")
            heading = section.get("heading")
            if not isinstance(heading, str) or not heading.strip():
                raise ContentGenerationProtocolError("AI content outline has an invalid section.")
            heading_key = KeywordDiscoveryRequestHandler._outline_heading_key(heading)
            # A normal canonical-title H2 is formatting noise because assembly
            # already creates the H1. A numbered listicle is different: its
            # first H2 is the promised list itself (for example, “10 ideas”),
            # and removing it produces a generic guide that never fulfils the
            # user-approved title.
            if (heading_key == canonical_key and not numbered_list_title) or heading_key in seen:
                continue
            seen.add(heading_key)
            usable.append(section)
        return usable

    @staticmethod
    def _numbered_listicle_count(title: str) -> int | None:
        """Return the promised item count for a real numbered ideas/list title."""
        match = re.match(r"^\s*(\d{1,2})\b", title)
        if not match:
            return None
        count = int(match.group(1))
        if not 1 <= count <= 50:
            return None
        normalized = title.casefold()
        english_list_words = r"\b(?:idea|ideas|way|ways|tip|tips|example|examples|design|designs|style|styles|option|options)\b"
        chinese_list_words = r"(?:个|种|条).{0,12}(?:创意|点子|方法|技巧|方案|设计)"
        return count if re.search(english_list_words, normalized) or re.search(chinese_list_words, title) else None

    @staticmethod
    def _normalise_outline_section(section: Mapping[str, Any], position: int) -> dict[str, Any]:
        heading = section.get("heading")
        if not isinstance(heading, str) or not heading.strip():
            raise ContentGenerationProtocolError("AI content outline has an invalid section.")
        purpose = section.get("purpose") if isinstance(section.get("purpose"), str) and section["purpose"].strip() else "Advance the reader decision."
        def strings(field: str) -> list[str]:
            value = section.get(field, [])
            return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []
        def strings_from(value: Any) -> list[str]:
            return [item.strip() for item in value if isinstance(item, str) and item.strip()][:4] if isinstance(value, list) else []
        level = section.get("level") if section.get("level") in {"h2", "h3"} else "h2"
        format_value = section.get("format") if section.get("format") in {"paragraphs", "list", "table"} else "paragraphs"
        raw_keywords = section.get("keyword_requirements") if isinstance(section.get("keyword_requirements"), Mapping) else {}
        raw_depth = section.get("depth_requirements") if isinstance(section.get("depth_requirements"), Mapping) else {}
        try: minimum_terms = int(raw_keywords.get("minimum_supporting_terms") or 0)
        except (TypeError, ValueError): minimum_terms = 0
        try: minimum_subtopics = int(raw_depth.get("minimum_subtopics") or 0)
        except (TypeError, ValueError): minimum_subtopics = 0
        return {
            "id": section.get("id") if isinstance(section.get("id"), str) and section["id"].strip() else f"s{position}",
            "heading": heading.strip(), "level": level,
            "reader_question": section.get("reader_question") if isinstance(section.get("reader_question"), str) else "",
            "purpose": purpose.strip(), "key_points": strings("key_points"), "source_ids": strings("source_ids"),
            "evidence_gaps": strings("evidence_gaps"), "format": format_value,
            "company_context_source_ids": strings("company_context_source_ids"),
            "company_context_role": section.get("company_context_role") if isinstance(section.get("company_context_role"), str) else "",
            "company_context_link_url": section.get("company_context_link_url") if isinstance(section.get("company_context_link_url"), str) else "",
            "keyword_requirements": {"supporting_terms": strings_from(raw_keywords.get("supporting_terms")), "minimum_supporting_terms": max(0, min(minimum_terms, 4))},
            "depth_requirements": {"minimum_subtopics": max(0, min(minimum_subtopics, 5)), "reader_outcome": str(raw_depth.get("reader_outcome") or "")[:300], "practical_detail": str(raw_depth.get("practical_detail") or "")[:300]},
            "target_words": max(0, int(section.get("target_words") or 0)),
        }

    @staticmethod
    def _outline_heading_key(value: str) -> str:
        return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)

    @staticmethod
    def _normalise_reader_markdown_format(markdown: str) -> str:
        """Canonicalize model formatting before storage or reader delivery.

        New prompts require Markdown, but this deterministic boundary also
        converts legacy/raw HTML tables and repairs loose ordered-list markers.
        """
        def clean_cell(fragment: str) -> str:
            value = re.sub(r"<(script|style|template|iframe|object|embed|svg|math)\b[^>]*>[\s\S]*?</\1\s*>", "", fragment, flags=re.IGNORECASE)
            value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
            value = re.sub(r"<[^>]+>", "", value)
            value = " ".join(html.unescape(value).split())
            return value.replace("|", "\\|")

        def convert_table(match: re.Match[str]) -> str:
            block = match.group(0)
            rows: list[list[str]] = []
            for row_block in re.findall(r"<tr\b[^>]*>([\s\S]*?)</tr\s*>", block, flags=re.IGNORECASE):
                row = [clean_cell(cell) for _tag, cell in re.findall(r"<(th|td)\b[^>]*>([\s\S]*?)</(?:th|td)\s*>", row_block, flags=re.IGNORECASE)]
                if row: rows.append(row)
            if not rows: return block
            width = max(len(row) for row in rows)
            rows = [row + [""] * (width - len(row)) for row in rows]
            header = rows[0]
            body = rows[1:]
            rendered = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
            rendered.extend("| " + " | ".join(row) + " |" for row in body)
            return "\n".join(rendered)

        cleaned = re.sub(r"<table\b[^>]*>[\s\S]*?</table\s*>", convert_table, markdown, flags=re.IGNORECASE)
        lines = cleaned.splitlines()
        active_indent: str | None = None
        next_number = 1
        for index, line in enumerate(lines):
            match = re.match(r"^(\s*)(\d+)([.)])(\s+.+)$", line)
            if match:
                indent = match.group(1)
                if active_indent != indent:
                    active_indent = indent
                    next_number = int(match.group(2))
                lines[index] = f"{indent}{next_number}.{match.group(4)}"
                next_number += 1
            elif line.strip():
                active_indent = None
                next_number = 1
        return "\n".join(lines)

    @staticmethod
    def _sanitize_reader_markdown(markdown: str) -> str:
        """Keep internal evidence and quality-gate markers out of reader content."""
        cleaned = re.sub(r"\s*\[competitor-[a-z0-9_-]+\]", "", markdown, flags=re.IGNORECASE)
        # Earlier drafts used [VERIFY] beside an unsupported claim.  The
        # unresolved item remains in the database and still blocks publishing,
        # but the reader sees neither the marker nor a verification note.
        cleaned = re.sub(r"\s*\[(?:verify|verification)(?:\s*:\s*[^\]]*)?\]", "", cleaned, flags=re.IGNORECASE)
        cleaned = KeywordDiscoveryRequestHandler._remove_internal_verification_columns(cleaned)
        cleaned = KeywordDiscoveryRequestHandler._normalise_reader_markdown_format(cleaned)
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    @staticmethod
    def _remove_internal_verification_columns(markdown: str) -> str:
        """Remove legacy Markdown table columns used only for verification notes."""
        lines = markdown.splitlines()

        def cells(value: str) -> list[str]:
            return [item.strip() for item in re.split(r"(?<!\\)\|", value.strip().strip("|"))]

        def row(values: list[str]) -> str:
            return "| " + " | ".join(values) + " |"

        index = 0
        internal_headers = {"verify", "verification", "verification status", "verification notes"}
        while index + 1 < len(lines):
            header = cells(lines[index]) if "|" in lines[index] else []
            divider = cells(lines[index + 1]) if "|" in lines[index + 1] else []
            is_divider = bool(divider) and len(header) == len(divider) and all(
                re.fullmatch(r":?-{3,}:?", value.replace(" ", "")) is not None for value in divider
            )
            internal_indexes = [
                position for position, value in enumerate(header)
                if re.sub(r"[*_`]+", "", value).strip().casefold() in internal_headers
            ]
            if not (is_divider and internal_indexes):
                index += 1
                continue
            keep = [position for position in range(len(header)) if position not in internal_indexes]
            end = index + 2
            while end < len(lines) and lines[end].strip() and "|" in lines[end]:
                end += 1
            rows = [cells(line) for line in lines[index + 2:end]]
            if len(keep) >= 2:
                lines[index:end] = [
                    row([header[position] for position in keep]),
                    row(["---" for _ in keep]),
                    *[row([values[position] if position < len(values) else "" for position in keep]) for values in rows],
                ]
                index += len(rows) + 2
            else:
                # A one-column table is not valid Markdown; retain its useful
                # reader value as a short list instead of an empty audit table.
                replacement = [f"- {values[keep[0]].strip()}" for values in rows if keep and keep[0] < len(values) and values[keep[0]].strip()]
                lines[index:end] = replacement
                index += len(replacement)
        return "\n".join(lines)

    @staticmethod
    def _english_word_count(markdown: str) -> int:
        return len(re.findall(r"\b[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?\b", markdown))

    @staticmethod
    def _bold_primary_keyword_count(markdown: str, keyword: str) -> int:
        if not keyword.strip():
            return 0
        return len(re.findall(rf"\*\*{re.escape(keyword.strip())}\*\*", markdown, flags=re.IGNORECASE))

    @staticmethod
    def _normalise_primary_keyword_emphasis(markdown: str, keyword: str) -> str:
        """Keep exactly one emphasized exact-match keyword in body prose.

        H2 chapters are generated independently, so a model may follow the
        global emphasis instruction once per chapter.  Normalize only the
        Markdown markers; preserve the original wording and capitalization.
        """
        keyword = keyword.strip()
        if not keyword:
            return markdown
        pattern = re.compile(rf"\*\*({re.escape(keyword)})\*\*", flags=re.IGNORECASE)
        cleaned = pattern.sub(r"\1", markdown)
        exact = re.compile(rf"(?<![\w*])({re.escape(keyword)})(?![\w*])", flags=re.IGNORECASE)
        lines = cleaned.splitlines()
        for index, line in enumerate(lines):
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#") or stripped.startswith("|"):
                continue
            match = exact.search(line)
            if match is None:
                continue
            lines[index] = f"{line[:match.start()]}**{match.group(1)}**{line[match.end():]}"
            break
        return "\n".join(lines)

    @staticmethod
    def _authority_evidence_context(markdown: str, *, max_chars: int = 4_000) -> str:
        """Keep authority-link planning small and tied to actual article claims.

        Sending a whole long article to a proxy model is both slow and less
        precise.  This creates a bounded dossier from the article's H2s and
        their opening factual material; the model proposes links only for
        those claims and every proposed URL is still fetched and verified.
        """
        clean = KeywordDiscoveryRequestHandler._sanitize_reader_markdown(markdown)
        chunks = re.split(r"(?m)^##\s+", clean)
        dossier: list[str] = []
        for chunk in chunks[1:]:
            heading, _, body = chunk.partition("\n")
            heading = " ".join(heading.split())[:180]
            body = re.sub(r"(?m)^###\s+", "", body)
            body = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", body)
            body = " ".join(body.split())
            if heading and body:
                dossier.append(f"H2: {heading}\nEvidence: {body[:560]}")
            if len(dossier) >= 6:
                break
        if not dossier:
            fallback = " ".join(clean.split())[:3_000]
            dossier.append(f"Article evidence: {fallback}")
        return "\n\n".join(dossier)[:max_chars]

    @staticmethod
    def _assembly_fragment(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        # The framing model must not create a second H1/H2 around the locally
        # preserved deep chapters.
        return re.sub(r"^\s{0,3}#{1,2}\s+[^\n]+\n+", "", value.strip())

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
            availability = "available" if item.get("availability") == "provided" else item.get("availability")
            if not isinstance(source_id, str) or not source_id.strip():
                raise ValueError("structured source.source_id is required")
            if not isinstance(source_type, str) or not source_type.strip():
                raise ValueError("structured source.source_type is required")
            if availability not in {"available", "unavailable"}:
                raise ValueError("structured source.availability must be available or unavailable")
            item["availability"] = availability
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
        industry = self._optional_text(payload, "industry") or ""
        site_url = self._optional_text(payload, "site_url") or name
        try:
            project = self.server.project_repository.create_project(
                name=name, site_url=site_url, industry=industry,
                country_code=country, language_code=language,
            )
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.CREATED, {"id": project["id"]})

    def _list_projects(self) -> None:
        self._json(HTTPStatus.OK, self.server.project_repository.list_projects())

    def _list_project_summaries(self) -> None:
        self._json(HTTPStatus.OK, self.server.project_repository.list_project_summaries())

    def _runtime_database_status(self) -> None:
        self._json(HTTPStatus.OK, self.server.runtime_database.status())

    def _runtime_database_action(self, action: str, payload: Mapping[str, Any]) -> None:
        confirmation = str(payload.get("confirmation") or "")
        try:
            if action == "shadow/enable":
                result = self.server.runtime_database.enable_shadow(confirmation=confirmation)
            elif action == "cutover/check":
                result = self.server.runtime_database.check_cutover(
                    credentials_reauthorized=payload.get("credentials_reauthorized") is True
                )
            elif action == "cutover":
                result = self.server.runtime_database.cutover(
                    confirmation=confirmation,
                    credentials_reauthorized=payload.get("credentials_reauthorized") is True,
                )
            else:
                result = self.server.runtime_database.rollback(confirmation=confirmation)
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except RuntimeError as error:
            self._json(HTTPStatus.CONFLICT, {"error": str(error), "gate": self.server.runtime_database.status().get("last_gate")})
            return
        self._json(HTTPStatus.OK, result)

    def _list_system_tasks(self) -> None:
        with self._database() as connection:
            rows = connection.execute(
                """SELECT 'keyword' AS task_type,id,project_id,status,COALESCE(updated_at,created_at) AS updated_at,COALESCE(failure_reason,'') AS message FROM keyword_research_tasks
                   UNION ALL SELECT 'title',id,project_id,status,COALESCE(completed_at,started_at,created_at),COALESCE(error_summary,'') FROM title_generation_jobs
                   UNION ALL SELECT 'content',id,project_id,status,COALESCE(completed_at,started_at,''),COALESCE(error_summary,'') FROM content_generation_runs
                   UNION ALL SELECT 'website_crawl',id,project_id,status,COALESCE(completed_at,created_at),COALESCE(failure_reason,message,'') FROM project_knowledge_crawl_runs
                   UNION ALL SELECT 'wordpress_publish',id,project_id,status,created_at,COALESCE(error_summary,'') FROM content_wordpress_publications
                   UNION ALL SELECT 'competitor_collection',id,project_id,status,updated_at,COALESCE(error_summary,'') FROM competitor_catalog_collection_runs
                   UNION ALL SELECT 'competitor_content_learning',id,project_id,status,updated_at,COALESCE(error_summary,'') FROM competitor_content_learning_runs
                   UNION ALL SELECT 'competitor_learning',id,project_id,status,COALESCE(completed_at,started_at,created_at),COALESCE(error_summary,topic,'') FROM competitor_learning_runs
                   UNION ALL SELECT 'agent',id,project_id,status,updated_at,COALESCE(error_summary,'') FROM agent_jobs
                   UNION ALL SELECT 'durable_queue',id,project_id,status,updated_at,
                       task_type || ' #' || resource_id || ' · 尝试 ' || attempt_count || '/' || max_attempts ||
                       CASE WHEN trim(last_error)<>'' THEN ' · ' || last_error ELSE '' END
                       FROM durable_task_queue
                   ORDER BY updated_at DESC LIMIT 100"""
            ).fetchall()
            project_names = {row["id"]: row["name"] for row in connection.execute("SELECT id,name FROM projects").fetchall()}
        payload = [{**dict(row), "project_name": project_names.get(row["project_id"], f"项目 #{row['project_id']}")} for row in rows]
        self._json(HTTPStatus.OK, payload)

    def _execute_durable_queue_job(self, queue_job_id: int) -> None:
        """Claim one delivery and invoke its existing project-scoped workflow."""
        supplied_token = self.headers.get("X-SEO-Worker-Token", "")
        if not supplied_token or not hmac.compare_digest(supplied_token, self.server.worker_token):
            self._json(HTTPStatus.FORBIDDEN, {"error": "worker authentication failed"})
            return
        job = self.server.task_queue.claim(queue_job_id)
        if job is None:
            try:
                current = self.server.task_queue.get(queue_job_id)
            except ValueError as error:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                return
            self._json(HTTPStatus.OK, current)
            return
        try:
            project_id = int(job["project_id"])
            resource_id = int(job["resource_id"])
            task_type = str(job["task_type"])
            self._assert_queue_resource_project(task_type, resource_id, project_id)
            targets = {
                "competitor_catalog_collection": f"/api/competitor-url-catalog/collection-runs/{resource_id}/execute",
                "collected_competitor_learning": f"/api/competitor-content-learning/runs/{resource_id}/execute",
                "competitor_learning": f"/api/projects/{project_id}/competitor-learning/runs/{resource_id}/execute",
                "agent_job": f"/api/agent-jobs/{resource_id}/execute",
            }
            target = targets.get(task_type)
            if target is None:
                raise ValueError("queue task type is not executable")
            host, port = self.server.server_address[:2]
            callback_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
            request = Request(
                f"http://{callback_host}:{port}{target}",
                data=json.dumps({"project_id": project_id}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=1800) as response:
                if response.status >= 400:
                    raise RuntimeError(f"workflow endpoint returned HTTP {response.status}")
            result = self.server.task_queue.complete(queue_job_id)
        except Exception as error:
            result = self.server.task_queue.fail(queue_job_id, error)
        self._json(HTTPStatus.OK, result)

    def _assert_queue_resource_project(self, task_type: str, resource_id: int, project_id: int) -> None:
        tables = {
            "competitor_catalog_collection": "competitor_catalog_collection_runs",
            "collected_competitor_learning": "competitor_content_learning_runs",
            "competitor_learning": "competitor_learning_runs",
            "agent_job": "agent_jobs",
        }
        table = tables.get(task_type)
        if table is None:
            raise ValueError("queue task type is not supported")
        with self._database() as connection:
            self._project_exists(connection, project_id)
            if connection.execute(f"SELECT 1 FROM {table} WHERE id=? AND project_id=?", (resource_id, project_id)).fetchone() is None:
                raise ValueError("queue resource does not belong to this project")

    def _query_project_id(self) -> int | None:
        values = parse_qs(urlsplit(self.path).query).get("project_id", [])
        if len(values) != 1:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id query parameter is required."})
            return None
        try:
            project_id = int(values[0])
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id must be an integer."})
            return None
        if project_id < 1:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id must be positive."})
            return None
        return project_id

    @staticmethod
    def _agent_job_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(row)
        for field in ("input_json", "result_json", "checkpoint_json"):
            raw = value.pop(field, "{}")
            try:
                value[field.removesuffix("_json")] = json.loads(raw or "{}")
            except (TypeError, json.JSONDecodeError):
                value[field.removesuffix("_json")] = {}
        value["lifecycle_state"] = (
            "paused"
            if value.get("status") == "waiting_input" and value.get("current_node") == "paused"
            else value.get("status")
        )
        return value

    @staticmethod
    def _agent_step_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(row)
        for field in ("input_json", "output_json", "token_usage_json"):
            raw = value.pop(field, "{}")
            try:
                value[field.removesuffix("_json")] = json.loads(raw or "{}")
            except (TypeError, json.JSONDecodeError):
                value[field.removesuffix("_json")] = {}
        return value

    @staticmethod
    def _agent_tool_audit_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(row)
        for field in ("input_json", "output_json"):
            raw = value.pop(field, "{}")
            try:
                value[field.removesuffix("_json")] = json.loads(raw or "{}")
            except (TypeError, json.JSONDecodeError):
                value[field.removesuffix("_json")] = {}
        return value

    @staticmethod
    def _agent_approval_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(row)
        raw = value.pop("payload_json", "{}")
        try:
            value["payload"] = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            value["payload"] = {}
        return value

    def _agent_job_for_project(self, connection: sqlite3.Connection, job_id: int, project_id: int) -> sqlite3.Row:
        job = connection.execute("SELECT * FROM agent_jobs WHERE id=? AND project_id=?", (job_id, project_id)).fetchone()
        if job is None:
            raise ValueError("agent job does not exist in this project")
        return job

    def _create_agent_job(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        requested_action = self._text(payload, "requested_action")
        content_asset_id = None
        if "content_asset_id" in payload:
            content_asset_id = self._integer(payload, "content_asset_id")
            if content_asset_id is None:
                return
        if project_id is None or requested_action is None:
            return
        is_full_content_agent = requested_action == "full_content_agent"
        if is_full_content_agent and content_asset_id is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "content_asset_id is required for full_content_agent."})
            return
        approval_type = self._optional_text(payload, "approval_type")
        if approval_type not in {None, "blueprint", "publish"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "approval_type must be blueprint or publish."})
            return
        try:
            writer_provider, writer_model, reviewer_provider, reviewer_model = self._agent_model_route(payload)
            with self._database() as connection, connection:
                self._project_exists(connection, project_id)
                if content_asset_id is not None and connection.execute(
                    "SELECT 1 FROM content_assets WHERE id=? AND project_id=? AND deleted_at IS NULL",
                    (content_asset_id, project_id),
                ).fetchone() is None:
                    raise ValueError("content asset does not exist in this project")
                if is_full_content_agent and content_asset_id is not None:
                    self._require_content_competitor_learning(
                        connection, self._content_asset(connection, project_id, content_asset_id)
                    )
                stored_input = {
                        "requested_action": requested_action,
                        "content_asset_id": content_asset_id,
                        "writer_provider": writer_provider,
                        "writer_model": writer_model,
                        "reviewer_provider": reviewer_provider,
                        "reviewer_model": reviewer_model,
                        "target_audience": (self._optional_text(payload, "target_audience") or "")[:1000],
                        "business_goal": (self._optional_text(payload, "business_goal") or "")[:1000],
                        "voice": (self._optional_text(payload, "voice") or "")[:1000],
                        "cta": (self._optional_text(payload, "cta") or "")[:1000],
                        "brand": (self._optional_text(payload, "brand") or "")[:1000],
                        "sources": self._content_sources(payload.get("sources", [])),
                        "constraints": [str(item)[:500] for item in payload.get("constraints", [])[:20] if isinstance(item, str)] if isinstance(payload.get("constraints"), list) else [],
                    }
                cursor = connection.execute(
                    """INSERT INTO agent_jobs(
                           project_id,content_asset_id,requested_action,status,current_node,workflow_version,input_json
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        project_id, content_asset_id, requested_action, "planning", "created",
                        "content-agent-v2" if is_full_content_agent else "content-agent-v1",
                        json.dumps(stored_input, ensure_ascii=False),
                    ),
                )
                job_id = int(cursor.lastrowid)
                tools = AgentToolService(connection, project_id=project_id, job_id=job_id)
                workflow = run_content_workflow(
                    project_id=project_id,
                    content_asset_id=content_asset_id,
                    requested_action=requested_action,
                    tool_invoker=lambda name, value: tools.invoke(name, value).output,
                )
                audits = {
                    row["tool_name"]: row
                    for row in connection.execute("SELECT * FROM agent_tool_audits WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
                }
                for event in workflow["events"]:
                    audit = audits.get(event["node"])
                    output = {}
                    if event["node"] == "load_project_context":
                        output = workflow.get("project_context", {})
                    elif event["node"] == "retrieve_project_memories":
                        output = {"memories": workflow.get("retrieved_memories", [])}
                    connection.execute(
                        """INSERT INTO agent_steps(job_id,node_name,status,input_summary,output_json,duration_ms,started_at,completed_at)
                           VALUES(?,?, 'completed', ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                        (job_id, event["node"], event["message"], json.dumps(output or {"message": event["message"]}, ensure_ascii=False), int(audit["duration_ms"]) if audit else 0),
                    )
                initial_status = "waiting_approval" if approval_type else str(workflow["status"])
                checkpoint = {
                    "current_node": workflow["current_node"],
                    "project_context": workflow.get("project_context", {}),
                    "retrieved_memory_ids": [item.get("memory_id") for item in workflow.get("retrieved_memories", [])],
                    "retrieved_memories": workflow.get("retrieved_memories", []),
                }
                connection.execute(
                    """UPDATE agent_jobs SET status=?,current_node=?,result_json=?,checkpoint_json=?,updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND project_id=?""",
                    (
                        initial_status,
                        str(workflow["current_node"]),
                        json.dumps({"project_context": workflow.get("project_context", {}), "retrieved_memories": workflow.get("retrieved_memories", [])}, ensure_ascii=False),
                        json.dumps(checkpoint, ensure_ascii=False),
                        job_id,
                        project_id,
                    ),
                )
                approval_id = None
                if approval_type:
                    approval_payload = payload.get("approval_payload") if isinstance(payload.get("approval_payload"), Mapping) else {}
                    cursor = connection.execute(
                        "INSERT INTO agent_approval_requests(project_id,job_id,approval_type,payload_json) VALUES(?,?,?,?)",
                        (project_id, job_id, approval_type, json.dumps(dict(approval_payload), ensure_ascii=False)),
                    )
                    approval_id = int(cursor.lastrowid)
                row = self._agent_job_for_project(connection, job_id, project_id)
        except (AgentWorkflowInputError, sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        response = self._agent_job_payload(row)
        if approval_id is not None:
            response["approval_id"] = approval_id
        self._json(HTTPStatus.CREATED, response)
        if is_full_content_agent:
            self.server.enqueue_background_task("agent_job", project_id, job_id)

    @staticmethod
    def _agent_model_route(payload: Mapping[str, Any]) -> tuple[str, str | None, str, str | None]:
        """Validate only model metadata; providers are constructed by backend nodes later."""
        writer_provider = payload.get("writer_provider", payload.get("provider", "openai"))
        reviewer_provider = payload.get("reviewer_provider", writer_provider)
        if writer_provider not in AI_PROVIDERS or reviewer_provider not in AI_PROVIDERS:
            raise ValueError("writer_provider and reviewer_provider must be supported AI providers")
        writer_model = payload.get("writer_model", payload.get("model"))
        reviewer_model = payload.get("reviewer_model", writer_model)
        for field, value in (("writer_model", writer_model), ("reviewer_model", reviewer_model)):
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", value.strip())):
                raise ValueError(f"{field} contains unsupported characters")
        return str(writer_provider), writer_model.strip() if isinstance(writer_model, str) else None, str(reviewer_provider), reviewer_model.strip() if isinstance(reviewer_model, str) else None

    def _list_agent_jobs(self) -> None:
        project_id = self._query_project_id()
        if project_id is None:
            return
        try:
            with self._database() as connection:
                self._project_exists(connection, project_id)
                rows = connection.execute("SELECT * FROM agent_jobs WHERE project_id=? ORDER BY updated_at DESC,id DESC", (project_id,)).fetchall()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, [self._agent_job_payload(row) for row in rows])

    def _get_agent_job(self, job_id: int) -> None:
        project_id = self._query_project_id()
        if project_id is None:
            return
        try:
            with self._database() as connection:
                job = self._agent_job_for_project(connection, job_id, project_id)
                steps = connection.execute("SELECT * FROM agent_steps WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
                approvals = connection.execute("SELECT * FROM agent_approval_requests WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
                tool_audits = connection.execute(
                    "SELECT * FROM agent_tool_audits WHERE job_id=? AND project_id=? ORDER BY id",
                    (job_id, project_id),
                ).fetchall()
                basis_report = connection.execute(
                    "SELECT * FROM content_generation_basis_reports WHERE agent_job_id=? AND project_id=?",
                    (job_id, project_id),
                ).fetchone()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        value = self._agent_job_payload(job)
        value["steps"] = [self._agent_step_payload(row) for row in steps]
        value["approvals"] = [self._agent_approval_payload(row) for row in approvals]
        value["tool_audits"] = [self._agent_tool_audit_payload(row) for row in tool_audits]
        if basis_report is not None:
            try:
                value["basis_report"] = json.loads(basis_report["report_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                value["basis_report"] = {}
        else:
            value["basis_report"] = None
        self._json(HTTPStatus.OK, value)

    @staticmethod
    def _content_learning_memory_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(row)
        raw = value.pop("evidence_json", "{}")
        try:
            value["evidence"] = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            value["evidence"] = {}
        raw_applicability = value.pop("applicability_json", "{}")
        try:
            value["applicability"] = json.loads(raw_applicability or "{}")
        except (TypeError, json.JSONDecodeError):
            value["applicability"] = {}
        return value

    def _memory_for_project(self, connection: sqlite3.Connection, memory_id: int, project_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM content_learning_memories WHERE id=? AND project_id=?", (memory_id, project_id)).fetchone()
        if row is None:
            raise ValueError("content learning memory does not exist in this project")
        return row

    def _list_content_learning_memories(self) -> None:
        project_id = self._query_project_id()
        if project_id is None:
            return
        try:
            with self._database() as connection:
                self._project_exists(connection, project_id)
                rows = connection.execute(
                    """SELECT * FROM content_learning_memories WHERE project_id=?
                       ORDER BY pinned DESC,manual_priority DESC,quality_score DESC,updated_at DESC,id DESC""",
                    (project_id,),
                ).fetchall()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.OK, [self._content_learning_memory_payload(row) for row in rows])

    def _get_content_learning_memory(self, memory_id: int) -> None:
        project_id = self._query_project_id()
        if project_id is None:
            return
        try:
            with self._database() as connection:
                row = self._memory_for_project(connection, memory_id, project_id)
                links = connection.execute(
                    """SELECT links.*,assets.title_snapshot,assets.project_id AS asset_project_id
                       FROM content_memory_links links JOIN content_assets assets ON assets.id=links.content_asset_id
                       WHERE links.memory_id=? AND assets.project_id=? ORDER BY links.relevance_score DESC,links.id DESC""",
                    (memory_id, project_id),
                ).fetchall()
                feedback = connection.execute(
                    "SELECT id,decision,note,created_at FROM content_learning_memory_feedback WHERE project_id=? AND memory_id=? ORDER BY id DESC",
                    (project_id, memory_id),
                ).fetchall()
                sources = connection.execute(
                    """SELECT source_type,source_id,source_url,source_content_hash,source_version_id,
                              evidence_excerpt,captured_at
                       FROM content_learning_memory_sources
                       WHERE project_id=? AND memory_id=? ORDER BY captured_at DESC,id DESC""",
                    (project_id, memory_id),
                ).fetchall()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        value = self._content_learning_memory_payload(row)
        value["content_links"] = [dict(link) for link in links]
        value["feedback"] = [dict(item) for item in feedback]
        value["sources"] = [dict(item) for item in sources]
        self._json(HTTPStatus.OK, value)

    def _create_content_learning_memory(self, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        memory_type = self._text(payload, "memory_type")
        topic = self._text(payload, "topic")
        summary = self._text(payload, "summary")
        if project_id is None or memory_type is None or topic is None or summary is None:
            return
        if memory_type not in {"style", "brand", "fact", "performance", "editorial"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "unsupported memory_type"}); return
        score = payload.get("quality_score", 0)
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= float(score) <= 1:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "quality_score must be between 0 and 1."}); return
        evidence = payload.get("evidence", {})
        if not isinstance(evidence, Mapping):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "evidence must be an object."}); return
        try:
            with self._database() as connection, connection:
                self._project_exists(connection, project_id)
                cursor = connection.execute("""INSERT INTO content_learning_memories(project_id,memory_type,topic,summary,evidence_json,source_url,source_content_hash,quality_score)
                    VALUES(?,?,?,?,?,?,?,?)""", (project_id, memory_type, topic[:300], summary[:12000], json.dumps(dict(evidence), ensure_ascii=False), (self._optional_text(payload, "source_url") or "")[:2000], (self._optional_text(payload, "source_content_hash") or "")[:128], float(score)))
                row = self._memory_for_project(connection, int(cursor.lastrowid), project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.CREATED, self._content_learning_memory_payload(row))

    def _set_content_learning_memory_status(self, memory_id: int, action: str, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        status = "disabled" if action == "disable" else "active"
        try:
            with self._database() as connection, connection:
                self._memory_for_project(connection, memory_id, project_id)
                connection.execute("UPDATE content_learning_memories SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, memory_id))
                row = self._memory_for_project(connection, memory_id, project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.OK, self._content_learning_memory_payload(row))

    def _set_content_learning_memory_pin(self, memory_id: int, action: str, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection, connection:
                self._memory_for_project(connection, memory_id, project_id)
                connection.execute(
                    "UPDATE content_learning_memories SET pinned=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (1 if action == "pin" else 0, memory_id),
                )
                row = self._memory_for_project(connection, memory_id, project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.OK, self._content_learning_memory_payload(row))

    def _update_content_learning_memory(self, memory_id: int, payload: Mapping[str, Any]) -> None:
        """Apply human strategy edits without allowing source evidence to be rewritten."""
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        topic, summary = self._optional_text(payload, "topic"), self._optional_text(payload, "summary")
        priority = payload.get("manual_priority")
        if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool) or not -10 <= priority <= 10):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "manual_priority must be an integer between -10 and 10."}); return
        if topic is None and summary is None and priority is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Provide topic, summary, or manual_priority."}); return
        if topic is not None and not topic.strip():
            self._json(HTTPStatus.BAD_REQUEST, {"error": "topic cannot be empty."}); return
        if summary is not None and not summary.strip():
            self._json(HTTPStatus.BAD_REQUEST, {"error": "summary cannot be empty."}); return
        try:
            with self._database() as connection, connection:
                row = self._memory_for_project(connection, memory_id, project_id)
                updates: list[tuple[str, Any]] = []
                # Performance evidence is produced by GSC and intentionally stays immutable.
                # The editable topic/summary are strategy labels, not the evidence payload.
                if topic is not None: updates.append(("topic", topic.strip()[:300]))
                if summary is not None: updates.append(("summary", summary.strip()[:12000]))
                if priority is not None: updates.append(("manual_priority", priority))
                statement = ",".join(f"{field}=?" for field, _value in updates) + ",updated_at=CURRENT_TIMESTAMP"
                connection.execute(f"UPDATE content_learning_memories SET {statement} WHERE id=?", (*[value for _field, value in updates], memory_id))
                row = self._memory_for_project(connection, memory_id, project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        self._json(HTTPStatus.OK, self._content_learning_memory_payload(row))

    def _add_content_learning_memory_feedback(self, memory_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        decision = self._optional_text(payload, "decision")
        note = self._optional_text(payload, "note") or ""
        if project_id is None:
            return
        if decision not in {"useful", "not_useful"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "decision must be useful or not_useful."}); return
        try:
            with self._database() as connection, connection:
                self._memory_for_project(connection, memory_id, project_id)
                connection.execute(
                    "INSERT INTO content_learning_memory_feedback(project_id,memory_id,decision,note) VALUES(?,?,?,?)",
                    (project_id, memory_id, decision, note.strip()[:2000]),
                )
                column = "positive_feedback_count" if decision == "useful" else "negative_feedback_count"
                connection.execute(
                    f"UPDATE content_learning_memories SET {column}={column}+1,last_feedback_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (memory_id,),
                )
                row = self._memory_for_project(connection, memory_id, project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
        result = self._content_learning_memory_payload(row)
        result["feedback_recorded"] = decision
        self._json(HTTPStatus.OK, result)

    @staticmethod
    def _memory_role(memory_type: str) -> str:
        # `editorial` is an internal kind of writing-style preference.  It is
        # deliberately linked as style, preserving the small stable role set
        # that generation uses when selecting context.
        return "style" if memory_type in {"style", "editorial"} else memory_type

    def _select_content_learning_memories(self, connection: sqlite3.Connection, asset: sqlite3.Row) -> list[dict[str, Any]]:
        """Select a small evidence-backed memory pack and record why it was used."""
        article_terms = {
            term.casefold() for term in re.findall(r"[A-Za-z0-9]{3,}", f"{asset['title_snapshot']} {asset['keyword'] or ''}")
        }
        rows = connection.execute(
            """SELECT * FROM content_learning_memories WHERE project_id=? AND status='active'
               ORDER BY pinned DESC,manual_priority DESC,quality_score DESC,updated_at DESC,id DESC""",
            (asset["project_id"],),
        ).fetchall()
        ranked: list[tuple[float, sqlite3.Row, list[str]]] = []
        for row in rows:
            memory_terms = {
                term.casefold() for term in re.findall(r"[A-Za-z0-9]{3,}", f"{row['topic']} {row['summary']}")
            }
            matched = sorted(article_terms & memory_terms)
            overlap = len(matched)
            if not matched:
                continue
            # A human can lift a memory that repeatedly helps, or demote one
            # that is technically valid but unsuitable.  Relevance remains a
            # hard guard: a pinned item with no topic overlap is never injected.
            helpful = min(int(row["positive_feedback_count"]), 8) * 0.015
            unhelpful = min(int(row["negative_feedback_count"]), 8) * 0.025
            priority = int(row["manual_priority"]) * 0.018
            pinned_bonus = 0.08 if int(row["pinned"]) else 0.0
            freshness = str(row["freshness_status"])
            freshness_factor = 1.0 if freshness == "current" else 0.82 if freshness == "needs_review" else 0.68
            score = (
                float(row["quality_score"]) * 0.45
                + float(row["confidence_score"]) * 0.15
                + min(overlap, 5) * 0.08 + priority + pinned_bonus + helpful - unhelpful
            ) * freshness_factor
            ranked.append((max(0.0, min(1.0, score)), row, matched))
        per_role_limit = {"style": 3, "brand": 2, "fact": 2, "performance": 2}
        selected: list[tuple[float, sqlite3.Row, list[str]]] = []
        used_by_role: dict[str, int] = {}
        for score, row, matched in sorted(ranked, key=lambda item: (item[0], item[1]["pinned"], item[1]["manual_priority"], item[1]["quality_score"]), reverse=True):
            role = self._memory_role(str(row["memory_type"]))
            if used_by_role.get(role, 0) >= per_role_limit[role] or len(selected) >= 7:
                continue
            selected.append((score, row, matched))
            used_by_role[role] = used_by_role.get(role, 0) + 1
        with connection:
            connection.execute("DELETE FROM content_memory_links WHERE content_asset_id=? AND selected_by_model=1 AND selected_by_user=0", (asset["id"],))
            for score, row, _matched in selected:
                connection.execute(
                    """INSERT INTO content_memory_links(content_asset_id,memory_id,role,relevance_score,selected_by_model)
                       VALUES(?,?,?,?,1)
                       ON CONFLICT(content_asset_id,memory_id,role) DO UPDATE SET relevance_score=excluded.relevance_score,selected_by_model=1""",
                    (asset["id"], row["id"], self._memory_role(str(row["memory_type"])), score),
                )
        result: list[dict[str, Any]] = []
        for score, row, matched in selected:
            value = self._content_learning_memory_payload(row)
            result.append({
                "memory_id": value["id"], "memory_type": value["memory_type"], "role": self._memory_role(str(value["memory_type"])),
                "topic": value["topic"], "summary": value["summary"], "evidence": value["evidence"],
                "card_type": value["card_type"], "confidence_score": value["confidence_score"],
                "freshness_status": value["freshness_status"], "evidence_count": value["evidence_count"],
                "applicability": value["applicability"], "source_url": value["source_url"],
                "relevance_score": round(score, 3),
                "selection_reason": f"主题词匹配：{', '.join(matched[:8])}；质量 {round(float(value['quality_score']) * 100)}；置信度 {round(float(value['confidence_score']) * 100)}；证据 {int(value['evidence_count'])} 条",
            })
        return result

    @staticmethod
    def _agent_input(row: Mapping[str, Any]) -> dict[str, Any]:
        try:
            value = json.loads(row["input_json"] or "{}")
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _write_content_generation_basis_report(
        self,
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        checkpoint: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist a compact explanation of the evidence used by this draft."""

        draft_id = checkpoint.get("draft_id")
        draft = connection.execute(
            "SELECT * FROM content_drafts WHERE id=? AND project_id=? AND content_asset_id=?",
            (draft_id, job["project_id"], job["content_asset_id"]),
        ).fetchone() if isinstance(draft_id, int) else None
        memories = checkpoint.get("retrieved_memories")
        memory_basis: list[dict[str, Any]] = []
        if isinstance(memories, list):
            for item in memories[:20]:
                if not isinstance(item, Mapping):
                    continue
                raw_sources = item.get("sources") if isinstance(item.get("sources"), list) else []
                memory_basis.append({
                    "memory_id": item.get("memory_id"),
                    "memory_type": item.get("memory_type"),
                    "card_type": item.get("card_type"),
                    "topic": str(item.get("topic") or "")[:300],
                    "selection_reason": str(item.get("selection_reason") or "")[:1000],
                    "source_url": str(item.get("source_url") or "")[:2000],
                    "sources": [{
                        "source_type": source.get("source_type"),
                        "source_id": source.get("source_id"),
                        "source_url": source.get("source_url"),
                        "source_content_hash": source.get("source_content_hash"),
                        "source_version_id": source.get("source_version_id"),
                        "captured_at": source.get("captured_at"),
                    } for source in raw_sources[:20] if isinstance(source, Mapping)],
                })
        unresolved: list[Any] = []
        qa: dict[str, Any] = {}
        if draft is not None:
            try:
                unresolved_value = json.loads(draft["unresolved_verify_json"] or "[]")
                unresolved = unresolved_value if isinstance(unresolved_value, list) else []
            except (TypeError, json.JSONDecodeError):
                unresolved = []
            try:
                qa_value = json.loads(draft["qa_json"] or "{}")
                qa = qa_value if isinstance(qa_value, dict) else {}
            except (TypeError, json.JSONDecodeError):
                qa = {}
        job_input = self._agent_input(job)
        report = {
            "agent_job_id": int(job["id"]),
            "project_id": int(job["project_id"]),
            "content_asset_id": int(job["content_asset_id"]),
            "brief_id": checkpoint.get("candidate_brief_id"),
            "outline_id": checkpoint.get("candidate_outline_id"),
            "draft_id": int(draft["id"]) if draft is not None else None,
            "generation_job_id": checkpoint.get("generation_job_id"),
            "memories": memory_basis,
            "qa": {
                "status": draft["qa_status"] if draft is not None else None,
                "checks": qa.get("checks", []),
                "targeted_rewrite": qa.get("targeted_rewrite", []),
                "rewrite_count": int(checkpoint.get("rewrite_count") or 0),
                "history": checkpoint.get("qa_history", []),
            },
            "unresolved_verify": unresolved[:50],
            "model_route": {
                "writer_provider": job_input.get("writer_provider"),
                "writer_model": job_input.get("writer_model"),
                "reviewer_provider": job_input.get("reviewer_provider"),
                "reviewer_model": job_input.get("reviewer_model"),
            },
        }
        connection.execute(
            """INSERT INTO content_generation_basis_reports(
                   project_id,agent_job_id,content_asset_id,draft_id,report_json
               ) VALUES(?,?,?,?,?)
               ON CONFLICT(agent_job_id) DO UPDATE SET
                   draft_id=excluded.draft_id,report_json=excluded.report_json,updated_at=CURRENT_TIMESTAMP""",
            (job["project_id"], job["id"], job["content_asset_id"], report["draft_id"], json.dumps(report, ensure_ascii=False)),
        )
        return report

    def _queue_gsc_feedback_learning(self, project_id: int, payload: Mapping[str, Any]) -> None:
        """Create a durable GSC feedback job and execute its first attempt."""

        raw_days = payload.get("days", 7)
        if not isinstance(raw_days, int) or isinstance(raw_days, bool):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "days must be an integer."})
            return
        window_days = max(7, min(365, raw_days))
        try:
            with self._database() as connection, connection:
                self._project_exists(connection, project_id)
                cursor = connection.execute(
                    """INSERT INTO agent_jobs(
                           project_id,requested_action,status,current_node,workflow_version,input_json
                       ) VALUES(?,'gsc_feedback_learning','queued','created','gsc-feedback-v1',?)""",
                    (project_id, json.dumps({
                        "requested_action": "gsc_feedback_learning",
                        "window_days": window_days,
                        "allowed_tools": [
                            "load_project_context", "capture_gsc_performance", "update_memory_governance",
                        ],
                    }, ensure_ascii=False)),
                )
                job_id = int(cursor.lastrowid)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self.server.enqueue_background_task("agent_job", project_id, job_id)
        self._json(HTTPStatus.ACCEPTED, {
            "job_id": job_id,
            "project_id": project_id,
            "status": "queued",
            "workflow_version": "gsc-feedback-v1",
            "message": "发布后效果学习已进入持久队列，可在系统任务中查看进度。",
        })

    def _execute_agent_job(self, job_id: int, payload: Mapping[str, Any]) -> None:
        """Dispatch an allowlisted durable workflow by its persisted identity."""

        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection:
                job = self._agent_job_for_project(connection, job_id, project_id)
                workflow = (str(job["requested_action"]), str(job["workflow_version"]))
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if workflow == ("gsc_feedback_learning", "gsc-feedback-v1"):
            self._execute_gsc_feedback_job(job_id, payload)
            return
        self._execute_content_agent_job(job_id, payload)

    def _execute_gsc_feedback_job(self, job_id: int, payload: Mapping[str, Any]) -> None:
        """Resume the GSC feedback workflow from its last committed node."""

        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection:
                job = self._agent_job_for_project(connection, job_id, project_id)
                if job["requested_action"] != "gsc_feedback_learning" or job["workflow_version"] != "gsc-feedback-v1":
                    raise ValueError("this agent job does not use the executable gsc-feedback-v1 workflow")
                if job["status"] == "completed":
                    result = self._agent_job_payload(job).get("result", {})
                    self._json(HTTPStatus.OK, result)
                    return
                if job["status"] in {"cancelled", "waiting_input", "waiting_approval"}:
                    self._json(HTTPStatus.OK, self._agent_job_payload(job))
                    return
                claimed = connection.execute(
                    """UPDATE agent_jobs SET status='running',started_at=COALESCE(started_at,CURRENT_TIMESTAMP),
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND project_id=? AND status IN ('planning','queued','retrying')""",
                    (job_id, project_id),
                )
                connection.commit()
                if claimed.rowcount != 1:
                    self._json(HTTPStatus.OK, self._agent_job_payload(self._agent_job_for_project(connection, job_id, project_id)))
                    return

                job = self._agent_job_for_project(connection, job_id, project_id)
                job_input = self._agent_input(job)
                checkpoint = self._agent_checkpoint(job)
                feedback = GscFeedbackService(connection, project_id=project_id)

                def capture_handler(tool_payload: Mapping[str, Any]) -> dict[str, Any]:
                    window_days = tool_payload.get("window_days", 7)
                    if not isinstance(window_days, int) or isinstance(window_days, bool):
                        raise ValueError("window_days must be an integer")
                    return feedback.capture_performance(window_days=window_days)

                def governance_handler(tool_payload: Mapping[str, Any]) -> dict[str, Any]:
                    snapshot_ids = tool_payload.get("snapshot_ids")
                    if not isinstance(snapshot_ids, list):
                        raise ValueError("snapshot_ids must be a list")
                    return feedback.update_memory_governance(snapshot_ids=snapshot_ids)

                tools = AgentToolService(connection, project_id=project_id, job_id=job_id, handlers={
                    "capture_gsc_performance": capture_handler,
                    "update_memory_governance": governance_handler,
                })

                def invoke_node(tool_name: str, node_name: str, tool_payload: Mapping[str, Any]) -> dict[str, Any]:
                    checkpoint["current_node"] = node_name
                    connection.execute(
                        "UPDATE agent_jobs SET current_node=?,checkpoint_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?",
                        (node_name, json.dumps(checkpoint, ensure_ascii=False), job_id, project_id),
                    )
                    attempt = int(connection.execute(
                        "SELECT COUNT(*) FROM agent_steps WHERE job_id=? AND node_name=?",
                        (job_id, node_name),
                    ).fetchone()[0]) + 1
                    step = connection.execute(
                        """INSERT INTO agent_steps(
                               job_id,node_name,attempt,status,input_summary,input_json,started_at
                           ) VALUES(?,?,?,'running',?,?,CURRENT_TIMESTAMP)""",
                        (job_id, node_name, attempt, f"Executing allowlisted tool {tool_name}.",
                         json.dumps({"project_id": project_id, **dict(tool_payload)}, ensure_ascii=False)),
                    )
                    connection.commit()
                    try:
                        call = tools.invoke(tool_name, {"project_id": project_id, **dict(tool_payload)})
                    except Exception as error:
                        connection.rollback()
                        connection.execute(
                            "UPDATE agent_steps SET status='failed',error_summary=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                            (str(error)[:2000], step.lastrowid),
                        )
                        connection.commit()
                        raise
                    output = call.output
                    checkpoint[node_name] = output
                    checkpoint["current_node"] = node_name
                    connection.execute(
                        """UPDATE agent_steps SET status='completed',output_json=?,duration_ms=?,
                               token_usage_json='{}',completed_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (json.dumps(output, ensure_ascii=False), call.duration_ms, step.lastrowid),
                    )
                    connection.execute(
                        "UPDATE agent_jobs SET checkpoint_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?",
                        (json.dumps(checkpoint, ensure_ascii=False), job_id, project_id),
                    )
                    connection.commit()
                    return output

                if not isinstance(checkpoint.get("load_project_context"), Mapping):
                    invoke_node("load_project_context", "load_project_context", {})
                if str(self._agent_job_for_project(connection, job_id, project_id)["status"]) != "running":
                    self._json(HTTPStatus.OK, self._agent_job_payload(self._agent_job_for_project(connection, job_id, project_id)))
                    return

                capture = checkpoint.get("capture_gsc_performance")
                if not isinstance(capture, Mapping):
                    capture = invoke_node(
                        "capture_gsc_performance", "capture_gsc_performance",
                        {"window_days": int(job_input.get("window_days") or 7)},
                    )
                if str(self._agent_job_for_project(connection, job_id, project_id)["status"]) != "running":
                    self._json(HTTPStatus.OK, self._agent_job_payload(self._agent_job_for_project(connection, job_id, project_id)))
                    return

                snapshots = capture.get("snapshots") if isinstance(capture.get("snapshots"), list) else []
                snapshot_ids = [
                    int(item["snapshot_id"]) for item in snapshots
                    if isinstance(item, Mapping) and isinstance(item.get("snapshot_id"), int)
                ]
                governance = checkpoint.get("update_memory_governance")
                if not isinstance(governance, Mapping):
                    governance = invoke_node(
                        "update_memory_governance", "update_memory_governance",
                        {"snapshot_ids": snapshot_ids},
                    )
                snapshot_memory_ids = governance.get("snapshot_memory_ids") if isinstance(governance.get("snapshot_memory_ids"), Mapping) else {}
                result_snapshots = []
                for item in snapshots:
                    if not isinstance(item, Mapping):
                        continue
                    value = dict(item)
                    value["memory_id"] = snapshot_memory_ids.get(str(value.get("snapshot_id")))
                    result_snapshots.append(value)
                result = {
                    "job_id": job_id,
                    "workflow_version": "gsc-feedback-v1",
                    "snapshots": result_snapshots,
                    "memories_created": int(governance.get("memories_created") or 0),
                    "message": "已保存发布内容的 GSC 快照。首个快照仅观察；至少 7 天后的第二次有效快照才形成可用于写作的表现记忆。",
                }
                checkpoint["current_node"] = "completed"
                connection.execute(
                    """UPDATE agent_jobs SET status='completed',current_node='completed',result_json=?,checkpoint_json=?,
                           error_summary=NULL,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND project_id=? AND status='running'""",
                    (json.dumps(result, ensure_ascii=False), json.dumps(checkpoint, ensure_ascii=False), job_id, project_id),
                )
                connection.commit()
                self._json(HTTPStatus.OK, result)
        except (sqlite3.Error, ValueError) as error:
            try:
                with self._database() as failure_connection, failure_connection:
                    failed = failure_connection.execute(
                        "SELECT * FROM agent_jobs WHERE id=? AND project_id=?", (job_id, project_id),
                    ).fetchone()
                    if failed is not None and failed["status"] not in {"cancelled", "waiting_input", "completed"}:
                        checkpoint = self._agent_checkpoint(failed)
                        node = str(checkpoint.get("current_node") or failed["current_node"] or "failed")
                        failure_connection.execute(
                            """UPDATE agent_jobs SET status='failed',current_node=?,error_summary=?,checkpoint_json=?,
                                   completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
                            (node, str(error)[:2000], json.dumps(checkpoint, ensure_ascii=False), job_id, project_id),
                        )
            except sqlite3.Error:
                pass
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error), "job_id": job_id})

    def _execute_content_agent_job(self, job_id: int, payload: Mapping[str, Any]) -> None:
        """Advance the P3 content Agent from its last durable checkpoint."""

        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection:
                job = self._agent_job_for_project(connection, job_id, project_id)
                if job["requested_action"] != "full_content_agent" or job["workflow_version"] != "content-agent-v2":
                    raise ValueError("this agent job does not use the executable content-agent-v2 workflow")
                if job["content_asset_id"] is None:
                    raise ValueError("content agent job has no content asset")
                if job["status"] in {"completed", "cancelled", "waiting_input"}:
                    self._json(HTTPStatus.OK, self._agent_job_payload(job))
                    return
                claimed = connection.execute(
                    """UPDATE agent_jobs SET status='running',started_at=COALESCE(started_at,CURRENT_TIMESTAMP),
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND project_id=? AND status IN ('planning','queued','retrying','waiting_approval')""",
                    (job_id, project_id),
                )
                connection.commit()
                if claimed.rowcount != 1:
                    current = self._agent_job_for_project(connection, job_id, project_id)
                    self._json(HTTPStatus.OK, self._agent_job_payload(current))
                    return

                job = self._agent_job_for_project(connection, job_id, project_id)
                job_input = self._agent_input(job)
                checkpoint = self._agent_checkpoint(job)
                asset = self._content_asset(connection, project_id, int(job["content_asset_id"]))
                writer_generator, writer_provider, writer_model = self._content_generator({
                    "provider": job_input.get("writer_provider", "openai"),
                    "model": job_input.get("writer_model"),
                })
                if writer_generator is None:
                    raise ValueError(f"{writer_provider} must be configured before the content Agent can run")
                reviewer_generator, reviewer_provider, reviewer_model = self._content_reviewer_generator(
                    writer_generator=writer_generator,
                    writer_provider=writer_provider,
                    writer_model=writer_model,
                    reviewer_provider=job_input.get("reviewer_provider"),
                    reviewer_model=job_input.get("reviewer_model"),
                )
                if reviewer_generator is None:
                    raise ValueError(f"{reviewer_provider} must be configured before Agent QA can run")

                generation_job_id = checkpoint.get("generation_job_id")
                if not isinstance(generation_job_id, int):
                    generation_job_id = self._start_content_generation_job(
                        connection, asset, "full_content_agent", writer_provider, writer_model,
                        reviewer_provider=reviewer_provider, reviewer_model=reviewer_model,
                        routing_mode="manual", routing_summary="Durable content-agent-v2 writer and independent reviewer route.",
                    )
                    checkpoint["generation_job_id"] = generation_job_id
                    connection.execute(
                        "UPDATE agent_jobs SET checkpoint_json=? WHERE id=? AND project_id=?",
                        (json.dumps(checkpoint, ensure_ascii=False), job_id, project_id),
                    )
                    connection.commit()

                def persist_checkpoint(node: str) -> None:
                    checkpoint["current_node"] = node
                    connection.execute(
                        "UPDATE agent_jobs SET current_node=?,checkpoint_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?",
                        (node, json.dumps(checkpoint, ensure_ascii=False), job_id, project_id),
                    )
                    connection.commit()

                def control_state() -> str:
                    connection.commit()
                    return str(self._agent_job_for_project(connection, job_id, project_id)["status"])

                def generate_blueprint(_tool_payload: Mapping[str, Any]) -> dict[str, Any]:
                    current_asset = self._content_asset(connection, project_id, int(job["content_asset_id"]))
                    if "original_asset_state" not in checkpoint:
                        checkpoint["original_asset_state"] = {
                            "status": current_asset["status"],
                            "current_brief_id": current_asset["current_brief_id"],
                            "current_outline_id": current_asset["current_outline_id"],
                            "current_draft_id": current_asset["current_draft_id"],
                            "current_generation_run_id": current_asset["current_generation_run_id"],
                        }
                        persist_checkpoint("generate_content_blueprint")
                    generation_payload = dict(job_input)
                    generation_payload["learning_memories"] = checkpoint.get("retrieved_memories", [])
                    try:
                        brief = self._generate_ai_brief(
                            connection, current_asset, generation_payload, writer_generator,
                            writer_provider, writer_model, generation_job_id, agent_job_id=job_id,
                        )
                        checkpoint["candidate_brief_id"] = int(brief["id"])
                        current_asset = self._content_asset(connection, project_id, int(job["content_asset_id"]))
                        outline = self._generate_ai_outline(
                            connection, current_asset, generation_payload, writer_generator,
                            writer_provider, writer_model, generation_job_id, agent_job_id=job_id,
                        )
                        checkpoint["candidate_outline_id"] = int(outline["id"])
                        # Blueprint review is automatic: retain the newly
                        # generated brief/outline as the live checkpoint, then
                        # proceed directly to the H2 batch. Publishing still
                        # requires its own human approval later.
                        connection.execute("UPDATE content_briefs SET status='current' WHERE id=?", (brief["id"],))
                        connection.execute("UPDATE content_outlines SET status='approved' WHERE id=?", (outline["id"],))
                        connection.execute(
                            """UPDATE content_assets SET status='outlining',current_brief_id=?,current_outline_id=?,
                                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
                            (brief["id"], outline["id"], job["content_asset_id"], project_id),
                        )
                        connection.commit()
                        return {"brief": brief, "outline": outline}
                    except Exception:
                        original = checkpoint.get("original_asset_state", {})
                        connection.execute("UPDATE content_briefs SET status='rejected' WHERE agent_job_id=?", (job_id,))
                        connection.execute("UPDATE content_outlines SET status='rejected' WHERE agent_job_id=?", (job_id,))
                        connection.execute(
                            """UPDATE content_assets SET status=?,current_brief_id=?,current_outline_id=?,
                                   current_draft_id=?,current_generation_run_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
                            (original.get("status", "planned"), original.get("current_brief_id"), original.get("current_outline_id"),
                             original.get("current_draft_id"), original.get("current_generation_run_id"), job["content_asset_id"], project_id),
                        )
                        connection.commit()
                        raise

                def generate_article(tool_payload: Mapping[str, Any]) -> dict[str, Any]:
                    current_asset = self._content_asset(connection, project_id, int(job["content_asset_id"]))
                    if tool_payload.get("mode") == "targeted_rewrite":
                        return {"draft": self._generate_ai_targeted_rewrite(
                            connection, current_asset, writer_generator, writer_provider, writer_model, generation_job_id,
                        )}
                    return {"draft": self._generate_ai_draft(
                        connection, current_asset, job_input, writer_generator, writer_provider, writer_model, generation_job_id,
                    )}

                def review_article(_tool_payload: Mapping[str, Any]) -> dict[str, Any]:
                    current_asset = self._content_asset(connection, project_id, int(job["content_asset_id"]))
                    return self._generate_ai_quality_review(
                        connection, current_asset, reviewer_generator, reviewer_provider,
                        reviewer_model, generation_job_id,
                    )

                tools = AgentToolService(connection, project_id=project_id, job_id=job_id, handlers={
                    "generate_content_blueprint": generate_blueprint,
                    "generate_article": generate_article,
                    "review_article": review_article,
                })

                def invoke_node(tool_name: str, node_name: str, tool_payload: Mapping[str, Any], provider: str, model: str | None) -> dict[str, Any]:
                    persist_checkpoint(node_name)
                    attempt = int(connection.execute(
                        "SELECT COUNT(*) FROM agent_steps WHERE job_id=? AND node_name=?", (job_id, node_name),
                    ).fetchone()[0]) + 1
                    cursor = connection.execute(
                        """INSERT INTO agent_steps(
                               job_id,node_name,attempt,status,model_provider,model,input_summary,input_json,prompt_version,started_at
                           ) VALUES(?,?,?,'running',?,?,?,?,?,CURRENT_TIMESTAMP)""",
                        (job_id, node_name, attempt, provider, model, f"Executing allowlisted tool {tool_name}.",
                         json.dumps({"project_id": project_id, "content_asset_id": job["content_asset_id"], "mode": tool_payload.get("mode")}, ensure_ascii=False), PROMPT_VERSION),
                    )
                    connection.commit()
                    try:
                        call = tools.invoke(tool_name, {"project_id": project_id, **dict(tool_payload)})
                    except Exception as error:
                        connection.execute(
                            "UPDATE agent_steps SET status='failed',error_summary=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                            (str(error)[:2000], cursor.lastrowid),
                        )
                        connection.commit()
                        raise
                    connection.execute(
                        """UPDATE agent_steps SET status='completed',output_json=?,duration_ms=?,token_usage_json='{}',
                               completed_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (json.dumps({"tool": tool_name, "status": "completed"}, ensure_ascii=False), call.duration_ms, cursor.lastrowid),
                    )
                    connection.commit()
                    return call.output

                if not isinstance(checkpoint.get("candidate_outline_id"), int):
                    blueprint = invoke_node(
                        "generate_content_blueprint", "generate_content_blueprint",
                        {"content_asset_id": job["content_asset_id"]}, writer_provider, writer_model,
                    )
                    checkpoint["blueprint"] = {
                        "brief_id": checkpoint.get("candidate_brief_id"),
                        "outline_id": checkpoint.get("candidate_outline_id"),
                        "outline": blueprint.get("outline"),
                    }
                    if control_state() in {"cancelled", "waiting_input"}:
                        self._json(HTTPStatus.OK, self._agent_job_payload(self._agent_job_for_project(connection, job_id, project_id)))
                        return
                if not checkpoint.get("blueprint_auto_approved"):
                    # Seamlessly migrate previously paused jobs that were
                    # created under the old manual-blueprint workflow.
                    brief_id = checkpoint.get("candidate_brief_id")
                    outline_id = checkpoint.get("candidate_outline_id")
                    if not isinstance(brief_id, int) or not isinstance(outline_id, int):
                        raise ContentGenerationProtocolError("content Agent has no usable blueprint checkpoint")
                    connection.execute("UPDATE content_briefs SET status='current' WHERE id=?", (brief_id,))
                    connection.execute("UPDATE content_outlines SET status='approved' WHERE id=?", (outline_id,))
                    connection.execute(
                        """UPDATE content_assets SET status='outlining',current_brief_id=?,current_outline_id=?,
                               updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
                        (brief_id, outline_id, job["content_asset_id"], project_id),
                    )
                    connection.execute(
                        """UPDATE agent_approval_requests SET status='approved',decided_by='system',decided_at=CURRENT_TIMESTAMP
                           WHERE project_id=? AND job_id=? AND approval_type='blueprint' AND status='pending'""",
                        (project_id, job_id),
                    )
                    checkpoint["blueprint_auto_approved"] = True
                    persist_checkpoint("generate_article")

                if not isinstance(checkpoint.get("draft_id"), int):
                    article_result = invoke_node(
                        "generate_article", "generate_article", {"content_asset_id": job["content_asset_id"], "mode": "draft"},
                        writer_provider, writer_model,
                    )
                    draft_value = article_result.get("draft") if isinstance(article_result.get("draft"), Mapping) else {}
                    checkpoint["draft_id"] = draft_value.get("id")
                    checkpoint["rewrite_count"] = int(checkpoint.get("rewrite_count") or 0)
                    persist_checkpoint("review_article")
                    if control_state() in {"cancelled", "waiting_input"}:
                        self._json(HTTPStatus.OK, self._agent_job_payload(self._agent_job_for_project(connection, job_id, project_id)))
                        return

                while True:
                    review_result = invoke_node(
                        "review_article", "review_article", {"content_asset_id": job["content_asset_id"], "draft_id": checkpoint.get("draft_id")},
                        reviewer_provider, reviewer_model,
                    )
                    review = review_result.get("review") if isinstance(review_result.get("review"), Mapping) else {}
                    checkpoint.setdefault("qa_history", []).append({
                        "draft_id": checkpoint.get("draft_id"),
                        "status": review.get("status"),
                        "rewrite_targets": len(review.get("targeted_rewrite", [])) if isinstance(review.get("targeted_rewrite"), list) else 0,
                    })
                    if review.get("status") == "approved":
                        persist_checkpoint("generation_basis_report")
                        current_job = self._agent_job_for_project(connection, job_id, project_id)
                        report = self._write_content_generation_basis_report(connection, current_job, checkpoint)
                        self._finish_content_generation_job(connection, generation_job_id, status="completed")
                        checkpoint["current_node"] = "completed"
                        connection.execute(
                            """UPDATE agent_jobs SET status='completed',current_node='completed',result_json=?,checkpoint_json=?,
                                   error_summary=NULL,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                               WHERE id=? AND project_id=? AND status='running'""",
                            (json.dumps({"draft_id": checkpoint.get("draft_id"), "basis_report": report}, ensure_ascii=False),
                             json.dumps(checkpoint, ensure_ascii=False), job_id, project_id),
                        )
                        connection.commit()
                        break
                    rewrite_targets = review.get("targeted_rewrite") if isinstance(review.get("targeted_rewrite"), list) else []
                    rewrite_count = int(checkpoint.get("rewrite_count") or 0)
                    if rewrite_targets and rewrite_count < 2:
                        if control_state() in {"cancelled", "waiting_input"}:
                            break
                        rewrite_result = invoke_node(
                            "generate_article", "targeted_rewrite", {"content_asset_id": job["content_asset_id"], "mode": "targeted_rewrite"},
                            writer_provider, writer_model,
                        )
                        rewritten = rewrite_result.get("draft") if isinstance(rewrite_result.get("draft"), Mapping) else {}
                        checkpoint["draft_id"] = rewritten.get("id")
                        checkpoint["rewrite_count"] = rewrite_count + 1
                        persist_checkpoint("review_article")
                        continue
                    persist_checkpoint("human_review_required")
                    current_job = self._agent_job_for_project(connection, job_id, project_id)
                    report = self._write_content_generation_basis_report(connection, current_job, checkpoint)
                    connection.execute(
                        """UPDATE agent_jobs SET status='waiting_input',current_node='human_review_required',result_json=?,
                               checkpoint_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=? AND status='running'""",
                        (json.dumps({"draft_id": checkpoint.get("draft_id"), "basis_report": report}, ensure_ascii=False),
                         json.dumps(checkpoint, ensure_ascii=False), job_id, project_id),
                    )
                    connection.commit()
                    break
                self._json(HTTPStatus.OK, self._agent_job_payload(self._agent_job_for_project(connection, job_id, project_id)))
        except (sqlite3.Error, ValueError, ContentGenerationProtocolError, CompetitorContentProtocolError) as error:
            try:
                with self._database() as connection, connection:
                    failed = connection.execute("SELECT * FROM agent_jobs WHERE id=? AND project_id=?", (job_id, project_id)).fetchone()
                    if failed is not None and failed["status"] not in {"cancelled", "waiting_input", "waiting_approval", "completed"}:
                        checkpoint = self._agent_checkpoint(failed)
                        node = str(checkpoint.get("current_node") or failed["current_node"] or "failed")
                        generation_job_id = checkpoint.get("generation_job_id")
                        if isinstance(generation_job_id, int):
                            self._finish_content_generation_job(connection, generation_job_id, status="failed", failed_stage=node, error_summary=str(error)[:2000])
                        connection.execute(
                            """UPDATE agent_jobs SET status='failed',current_node=?,error_summary=?,checkpoint_json=?,
                                   completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
                            (node, str(error)[:2000], json.dumps(checkpoint, ensure_ascii=False), job_id, project_id),
                        )
            except sqlite3.Error:
                pass
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def _retry_agent_job(self, job_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection, connection:
                job = self._agent_job_for_project(connection, job_id, project_id)
                if job["status"] not in {"failed", "cancelled"}:
                    raise ValueError("only failed or cancelled agent jobs can be retried")
                checkpoint = self._agent_checkpoint(job)
                resume_node = checkpoint.get("current_node")
                if not isinstance(resume_node, str) or not resume_node.strip():
                    resume_node = job["current_node"] if job["current_node"] not in {"failed", "cancelled"} else "prepare_workflow"
                retry_attempt = int(connection.execute(
                    "SELECT COUNT(*) FROM agent_steps WHERE job_id=? AND node_name='retry_requested'",
                    (job_id,),
                ).fetchone()[0]) + 1
                # An explicit retry supersedes any delivery lease left active
                # by the failed attempt. Without this, enqueue() deduplicates
                # against that stale row and the Agent can remain queued until
                # the old 35-minute lease expires.
                connection.execute(
                    """UPDATE durable_task_queue
                       SET status='failed',last_error='Superseded by an explicit Agent retry.',
                           lease_expires_at=NULL,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                       WHERE dedup_key=? AND status IN ('queued','running','retry_wait')""",
                    (f"agent_job:{project_id}:{job_id}",),
                )
                connection.execute(
                    """UPDATE agent_jobs SET status='queued',current_node=?,error_summary=NULL,
                       completed_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (resume_node, job_id),
                )
                connection.execute(
                    """INSERT INTO agent_steps(job_id,node_name,attempt,status,input_summary,output_json,started_at,completed_at)
                       VALUES(?, 'retry_requested', ?, 'completed', 'Retry was requested from the last durable state.', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                    (job_id, retry_attempt, json.dumps({"resume_node": resume_node}, ensure_ascii=False)),
                )
                row = self._agent_job_for_project(connection, job_id, project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, self._agent_job_payload(row))
        if row["requested_action"] == "full_content_agent" and row["workflow_version"] == "content-agent-v2":
            self.server.enqueue_background_task("agent_job", project_id, job_id)
        elif row["requested_action"] == "gsc_feedback_learning" and row["workflow_version"] == "gsc-feedback-v1":
            self.server.enqueue_background_task("agent_job", project_id, job_id)

    @staticmethod
    def _agent_checkpoint(row: Mapping[str, Any]) -> dict[str, Any]:
        try:
            checkpoint = json.loads(row["checkpoint_json"] or "{}")
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            checkpoint = {}
        return checkpoint if isinstance(checkpoint, dict) else {}

    def _pause_agent_job(self, job_id: int, payload: Mapping[str, Any]) -> None:
        """Persist a cooperative pause without discarding the resume node."""

        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection, connection:
                job = self._agent_job_for_project(connection, job_id, project_id)
                if job["status"] not in {"queued", "planning", "running", "retrying"}:
                    raise ValueError("only queued, planning, running, or retrying agent jobs can be paused")
                checkpoint = self._agent_checkpoint(job)
                checkpoint["pause"] = {
                    "resume_node": job["current_node"],
                    "previous_status": job["status"],
                }
                connection.execute(
                    """UPDATE agent_jobs SET status='waiting_input',current_node='paused',checkpoint_json=?,
                       updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
                    (json.dumps(checkpoint, ensure_ascii=False), job_id, project_id),
                )
                connection.execute(
                    """INSERT INTO agent_steps(job_id,node_name,status,input_summary,output_json,started_at,completed_at)
                       VALUES(?, 'pause_requested', 'completed', 'Pause was requested by the user.', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                    (job_id, json.dumps({"resume_node": job["current_node"]}, ensure_ascii=False)),
                )
                row = self._agent_job_for_project(connection, job_id, project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, self._agent_job_payload(row))

    def _resume_agent_job(self, job_id: int, payload: Mapping[str, Any]) -> None:
        """Queue a paused job from its last durable checkpoint."""

        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection, connection:
                job = self._agent_job_for_project(connection, job_id, project_id)
                if job["status"] != "waiting_input" or job["current_node"] != "paused":
                    raise ValueError("only paused agent jobs can be resumed")
                checkpoint = self._agent_checkpoint(job)
                pause = checkpoint.pop("pause", {})
                resume_node = pause.get("resume_node") if isinstance(pause, Mapping) else None
                if not isinstance(resume_node, str) or not resume_node.strip():
                    resume_node = checkpoint.get("current_node", "prepare_workflow")
                checkpoint["last_resumed_node"] = resume_node
                connection.execute(
                    """UPDATE agent_jobs SET status='queued',current_node=?,checkpoint_json=?,
                       completed_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
                    (resume_node, json.dumps(checkpoint, ensure_ascii=False), job_id, project_id),
                )
                connection.execute(
                    """INSERT INTO agent_steps(job_id,node_name,status,input_summary,output_json,started_at,completed_at)
                       VALUES(?, 'resume_requested', 'completed', 'Resume was requested from the last durable checkpoint.', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                    (job_id, json.dumps({"resume_node": resume_node}, ensure_ascii=False)),
                )
                row = self._agent_job_for_project(connection, job_id, project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, self._agent_job_payload(row))
        if row["requested_action"] == "full_content_agent" and row["workflow_version"] == "content-agent-v2":
            self.server.enqueue_background_task("agent_job", project_id, job_id)
        elif row["requested_action"] == "gsc_feedback_learning" and row["workflow_version"] == "gsc-feedback-v1":
            self.server.enqueue_background_task("agent_job", project_id, job_id)

    def _cancel_agent_job(self, job_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        if project_id is None:
            return
        try:
            with self._database() as connection, connection:
                job = self._agent_job_for_project(connection, job_id, project_id)
                if job["status"] in {"completed", "failed", "cancelled"}:
                    raise ValueError("completed, failed, or cancelled agent jobs cannot be cancelled")
                connection.execute("UPDATE agent_jobs SET status='cancelled',current_node='cancelled',completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
                connection.execute(
                    """INSERT INTO agent_steps(job_id,node_name,status,input_summary,output_json,started_at,completed_at)
                       VALUES(?, 'cancelled', 'cancelled', 'Cancellation was requested by the user.', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                    (job_id,),
                )
                row = self._agent_job_for_project(connection, job_id, project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, self._agent_job_payload(row))

    def _decide_agent_approval(self, approval_id: int, payload: Mapping[str, Any]) -> None:
        project_id = self._integer(payload, "project_id")
        decision = self._text(payload, "decision")
        if project_id is None or decision is None:
            return
        if decision not in {"approved", "rejected"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "decision must be approved or rejected."})
            return
        decided_by = self._optional_text(payload, "decided_by") or "user"
        try:
            with self._database() as connection, connection:
                approval = connection.execute("SELECT * FROM agent_approval_requests WHERE id=? AND project_id=?", (approval_id, project_id)).fetchone()
                if approval is None:
                    raise ValueError("approval request does not exist in this project")
                if approval["status"] != "pending":
                    raise ValueError("approval request has already been decided")
                job = self._agent_job_for_project(connection, int(approval["job_id"]), project_id)
                connection.execute("UPDATE agent_approval_requests SET status=?,decided_by=?,decided_at=CURRENT_TIMESTAMP WHERE id=?", (decision, decided_by[:100], approval_id))
                is_content_blueprint = (
                    approval["approval_type"] == "blueprint"
                    and job["requested_action"] == "full_content_agent"
                    and job["workflow_version"] == "content-agent-v2"
                )
                if is_content_blueprint:
                    checkpoint = self._agent_checkpoint(job)
                    brief_id = checkpoint.get("candidate_brief_id")
                    outline_id = checkpoint.get("candidate_outline_id")
                    if not isinstance(brief_id, int) or not isinstance(outline_id, int):
                        raise ValueError("content blueprint checkpoint is incomplete")
                    brief = connection.execute(
                        """SELECT briefs.id FROM content_briefs briefs
                           JOIN content_assets assets ON assets.id=briefs.content_asset_id
                           WHERE briefs.id=? AND briefs.agent_job_id=? AND assets.project_id=? AND assets.id=?""",
                        (brief_id, job["id"], project_id, job["content_asset_id"]),
                    ).fetchone()
                    outline = connection.execute(
                        """SELECT outlines.id FROM content_outlines outlines
                           JOIN content_assets assets ON assets.id=outlines.content_asset_id
                           WHERE outlines.id=? AND outlines.agent_job_id=? AND assets.project_id=? AND assets.id=?""",
                        (outline_id, job["id"], project_id, job["content_asset_id"]),
                    ).fetchone()
                    if brief is None or outline is None:
                        raise ValueError("content blueprint does not belong to this project and agent job")
                    if decision == "approved":
                        connection.execute(
                            "UPDATE content_briefs SET status='superseded' WHERE content_asset_id=? AND id<>? AND status='current'",
                            (job["content_asset_id"], brief_id),
                        )
                        connection.execute("UPDATE content_briefs SET status='current' WHERE id=?", (brief_id,))
                        connection.execute("UPDATE content_outlines SET status='approved' WHERE id=?", (outline_id,))
                        connection.execute(
                            """UPDATE content_assets SET status='outlining',current_brief_id=?,current_outline_id=?,
                                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
                            (brief_id, outline_id, job["content_asset_id"], project_id),
                        )
                        checkpoint["current_node"] = "blueprint_approved"
                        next_status = "queued"
                        next_node = "blueprint_approved"
                    else:
                        original = checkpoint.get("original_asset_state") if isinstance(checkpoint.get("original_asset_state"), Mapping) else {}
                        connection.execute("UPDATE content_briefs SET status='rejected' WHERE id=?", (brief_id,))
                        connection.execute("UPDATE content_outlines SET status='rejected' WHERE id=?", (outline_id,))
                        connection.execute(
                            """UPDATE content_assets SET status=?,current_brief_id=?,current_outline_id=?,current_draft_id=?,
                                   current_generation_run_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
                            (original.get("status", "planned"), original.get("current_brief_id"), original.get("current_outline_id"),
                             original.get("current_draft_id"), original.get("current_generation_run_id"), job["content_asset_id"], project_id),
                        )
                        generation_job_id = checkpoint.get("generation_job_id")
                        if isinstance(generation_job_id, int):
                            self._finish_content_generation_job(connection, generation_job_id, status="completed")
                        checkpoint["current_node"] = "blueprint_rejected"
                        next_status = "cancelled"
                        next_node = "blueprint_rejected"
                    connection.execute(
                        """UPDATE agent_jobs SET status=?,current_node=?,checkpoint_json=?,
                               completed_at=CASE WHEN ?='cancelled' THEN CURRENT_TIMESTAMP ELSE NULL END,
                               updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
                        (next_status, next_node, json.dumps(checkpoint, ensure_ascii=False), next_status, job["id"], project_id),
                    )
                else:
                    next_status = "queued" if decision == "approved" else "cancelled"
                    next_node = "approval_approved" if decision == "approved" else "approval_rejected"
                    connection.execute(
                        """UPDATE agent_jobs SET status=?,current_node=?,
                           completed_at=CASE WHEN ?='cancelled' THEN CURRENT_TIMESTAMP ELSE NULL END,
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (next_status, next_node, next_status, approval["job_id"]),
                    )
                connection.execute(
                    """INSERT INTO agent_steps(job_id,node_name,status,input_summary,output_json,started_at,completed_at)
                       VALUES(?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                    (approval["job_id"], next_node, "completed" if decision == "approved" else "cancelled", f"{approval['approval_type']} approval was {decision}.", json.dumps({"approval_id": approval_id, "decision": decision}, ensure_ascii=False)),
                )
                row = self._agent_job_for_project(connection, int(approval["job_id"]), project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, self._agent_job_payload(row))
        if decision == "approved" and row["requested_action"] == "full_content_agent" and row["workflow_version"] == "content-agent-v2":
            self.server.enqueue_background_task("agent_job", project_id, int(row["id"]))

    def _update_project(self, project_id: int, payload: Mapping[str, Any]) -> None:
        fields = {"name": self._optional_text(payload, "name"), "site_url": self._optional_text(payload, "site_url"), "industry": self._optional_text(payload, "industry"), "default_country": self._optional_text(payload, "country_code"), "default_language": self._optional_text(payload, "language_code")}
        updates = {key: value for key, value in fields.items() if value is not None}
        if not updates:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "At least one project field is required."})
            return
        try:
            row = self.server.project_repository.update_project(project_id, updates)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, row)

    def _delete_project(self, project_id: int) -> None:
        try:
            self.server.project_repository.delete_project(project_id)
            with self._credential_database() as credential_connection, credential_connection:
                credential_connection.execute(
                    "DELETE FROM wordpress_credentials WHERE project_id=?", (project_id,)
                )
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {"deleted": project_id})

    def _project_exists(self, connection: sqlite3.Connection, project_id: int) -> None:
        if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise ValueError("project does not exist")

    def _list_project_knowledge(self, project_id: int) -> None:
        try:
            with self._database() as connection, connection:
                self._project_exists(connection, project_id)
                rows = connection.execute(
                    """SELECT id,project_id,title,source_type,url,content,knowledge_type,status,summary,tags_json,classification_json,organizer_provider,organizer_model,created_at,updated_at
                       FROM project_knowledge_documents WHERE project_id=? ORDER BY updated_at DESC,id DESC""",
                    (project_id,),
                ).fetchall()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            try: value["tags"] = json.loads(value.pop("tags_json") or "[]")
            except (TypeError, json.JSONDecodeError): value["tags"] = []
            try: value["classification"] = json.loads(value.pop("classification_json") or "{}")
            except (TypeError, json.JSONDecodeError): value["classification"] = {}
            values.append(value)
        self._json(HTTPStatus.OK, values)

    def _create_project_knowledge(self, project_id: int, payload: Mapping[str, Any]) -> None:
        title = self._text(payload, "title")
        content = self._text(payload, "content")
        if title is None or content is None:
            return
        source_type = self._optional_text(payload, "source_type") or "manual"
        knowledge_type = self._optional_text(payload, "knowledge_type") or "other"
        url = self._optional_text(payload, "url") or ""
        try:
            with self._database() as connection, connection:
                self._project_exists(connection, project_id)
                organizer, organizer_provider, organizer_model = self._content_generator({"provider": "deepseek"})
                if organizer is None:
                    raise ValueError("DeepSeek must be configured before company knowledge can be organized.")
                organization_data = {
                    "title": title[:300], "url": url[:2000], "source_type": source_type[:40],
                    "user_selected_type": knowledge_type[:40], "content": content[:30_000],
                }
                raw_organization = organizer.run_stage(stage="knowledge_organization", data=organization_data) if callable(getattr(organizer, "run_stage", None)) else organizer.generate(stage="knowledge_organization", **organization_data)
                organization = json.loads(raw_organization) if isinstance(raw_organization, str) else raw_organization
                if not isinstance(organization, Mapping):
                    raise ContentGenerationProtocolError("DeepSeek knowledge organization returned invalid JSON.")
                organized_type = str(organization.get("knowledge_type") or knowledge_type).strip()
                if organized_type not in {"company", "product", "certification", "case_study", "faq", "other"}:
                    organized_type = knowledge_type
                tags = [str(item).strip()[:60] for item in organization.get("tags", []) if isinstance(item, str) and item.strip()][:6] if isinstance(organization.get("tags"), list) else []
                summary = str(organization.get("summary") or "").strip()[:2000]
                cursor = connection.execute(
                    """INSERT INTO project_knowledge_documents(project_id,title,source_type,url,content,knowledge_type,status,summary,tags_json,classification_json,organizer_provider,organizer_model)
                       VALUES(?,?,?,?,?,?, 'ready',?,?,?,?,?)""",
                    (project_id, title[:300], source_type[:40], url[:2000], content[:100000], organized_type[:40], summary, json.dumps(tags, ensure_ascii=False), json.dumps(dict(organization), ensure_ascii=False), organizer_provider, organizer_model),
                )
                row = connection.execute("SELECT * FROM project_knowledge_documents WHERE id=?", (cursor.lastrowid,)).fetchone()
        except (sqlite3.Error, ValueError, ContentGenerationProtocolError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        value = dict(row)
        try: value["tags"] = json.loads(value.pop("tags_json") or "[]")
        except (TypeError, json.JSONDecodeError): value["tags"] = []
        try: value["classification"] = json.loads(value.pop("classification_json") or "{}")
        except (TypeError, json.JSONDecodeError): value["classification"] = {}
        self._json(HTTPStatus.CREATED, value)

    def _delete_project_knowledge(self, project_id: int, document_id: int) -> None:
        try:
            with self._database() as connection, connection:
                cursor = connection.execute("DELETE FROM project_knowledge_documents WHERE id=? AND project_id=?", (document_id, project_id))
                if cursor.rowcount != 1:
                    raise ValueError("knowledge document does not exist in this project")
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {"deleted": document_id})

    def _crawl_project_knowledge(self, project_id: int, payload: Mapping[str, Any]) -> None:
        """Crawl a first-party site synchronously and preserve every page outcome.

        This handler intentionally stays in the single local service rather than
        calling platform_api over HTTP.  The crawler itself does concurrent page
        fetches, while this route records successful, skipped and failed pages.
        """
        max_pages = payload.get("max_pages", 20)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 80:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "max_pages must be an integer from 1 to 80."})
            return
        try:
            with self._database() as connection, connection:
                project = connection.execute("SELECT site_url,name FROM projects WHERE id=?", (project_id,)).fetchone()
                if project is None:
                    raise ValueError("project does not exist")
                site_url = str(project["site_url"] or project["name"] or "").strip()
                if not site_url:
                    raise ValueError("set a website address in project settings before crawling")
                cursor = connection.execute(
                    "INSERT INTO project_knowledge_crawl_runs(project_id,status,max_pages,message) VALUES(?, 'running', ?, 'Collecting first-party product and company pages.')",
                    (project_id, max_pages),
                )
                run_id = int(cursor.lastrowid)
            pages = crawl_site(site_url, max_pages)
            accepted = skipped = failed = 0
            with self._database() as connection, connection:
                for page in pages:
                    status = str(page.get("status") or "failed")
                    url = str(page.get("url") or "")[:2000]
                    if not url:
                        continue
                    title = str(page.get("title") or url)[:300]
                    kind = str(page.get("knowledge_type") or "other")[:40]
                    reason = str(page.get("reason") or "")[:1000]
                    connection.execute(
                        """INSERT INTO project_knowledge_crawl_pages(crawl_run_id,url,title,knowledge_type,status,reason)
                           VALUES(?,?,?,?,?,?) ON CONFLICT(crawl_run_id,url) DO UPDATE SET title=excluded.title,knowledge_type=excluded.knowledge_type,status=excluded.status,reason=excluded.reason""",
                        (run_id, url, title, kind, status, reason),
                    )
                    if status == "ready" and str(page.get("content") or "").strip():
                        existing = connection.execute("SELECT id FROM project_knowledge_documents WHERE project_id=? AND url=?", (project_id, url)).fetchone()
                        if existing:
                            connection.execute(
                                """UPDATE project_knowledge_documents SET title=?,source_type='website_crawl',content=?,knowledge_type=?,status='ready',updated_at=CURRENT_TIMESTAMP
                                   WHERE id=?""",
                                (title, str(page["content"])[:100000], kind, existing["id"]),
                            )
                        else:
                            connection.execute(
                                """INSERT INTO project_knowledge_documents(project_id,title,source_type,url,content,knowledge_type,status)
                                   VALUES(?,?,?,?,?,?, 'ready')""",
                                (project_id, title, "website_crawl", url, str(page["content"])[:100000], kind),
                            )
                        accepted += 1
                    elif status == "skipped":
                        skipped += 1
                    else:
                        failed += 1
                connection.execute(
                    """UPDATE project_knowledge_crawl_runs SET status='completed',discovered_count=?,accepted_count=?,skipped_count=?,failed_count=?,
                       message=?,completed_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (len(pages), accepted, skipped, failed, f"Collected {accepted} usable first-party knowledge pages.", run_id),
                )
        except Exception as error:
            if 'run_id' in locals():
                with self._database() as connection, connection:
                    connection.execute("UPDATE project_knowledge_crawl_runs SET status='failed',message='Website crawl failed.',failure_reason=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (str(error)[:1000], run_id))
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._get_knowledge_crawl_run(project_id, run_id)

    def _get_knowledge_crawl_run(self, project_id: int, run_id: int) -> None:
        try:
            with self._database() as connection:
                run = connection.execute("SELECT * FROM project_knowledge_crawl_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
                if run is None:
                    raise ValueError("knowledge crawl run does not exist in this project")
                pages = connection.execute("SELECT url,title,knowledge_type,status,reason FROM project_knowledge_crawl_pages WHERE crawl_run_id=? ORDER BY id", (run_id,)).fetchall()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {**dict(run), "pages": [dict(page) for page in pages]})

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

    def _get_serper_settings(self) -> None:
        self._json(HTTPStatus.OK, {"configured": _serper_api_key(self.server.ai_settings_path) is not None, "provider": "Serper.dev", "website": "https://serper.dev/"})

    def _get_image_generation_settings(self) -> None:
        configuration = _image_generation_configuration(self.server.ai_settings_path)
        image = _image_generation_settings(self.server.ai_settings_path)
        provider = _image_generation_provider(self.server.ai_settings_path)
        labels = {"openai": "ChatGPT 中转", "siliconflow": "硅基流动 Kolors"}
        self._json(HTTPStatus.OK, {
            "configured": configuration is not None,
            "base_url": configuration[2] if configuration else (SILICONFLOW_IMAGE_BASE_URL if provider == "siliconflow" else None),
            "model": _image_generation_model(self.server.ai_settings_path),
            "provider": provider,
            "provider_label": labels[provider],
            "api_key_saved": isinstance(image.get("api_key"), str) and bool(str(image.get("api_key")).strip()),
        })

    def _save_image_generation_settings(self, payload: Mapping[str, Any]) -> None:
        provider = self._text(payload, "provider")
        model = self._text(payload, "model")
        if provider is None or model is None: return
        if provider not in IMAGE_GENERATION_PROVIDERS:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "unsupported image provider"}); return
        try:
            document = dict(_ai_settings_document(self.server.ai_settings_path))
            integrations = dict(document.get("integrations")) if isinstance(document.get("integrations"), Mapping) else {}
            previous = integrations.get("image_generation") if isinstance(integrations.get("image_generation"), Mapping) else {}
            image_settings: dict[str, Any] = {"provider": provider, "model": model}
            if provider == "siliconflow":
                api_key = self._optional_text(payload, "api_key") or previous.get("api_key")
                if not isinstance(api_key, str) or not api_key.strip():
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "硅基流动 API Key is required the first time you save this provider."}); return
                image_settings["api_key"] = api_key.strip()
            integrations["image_generation"] = image_settings
            document["integrations"] = integrations
            self.server.ai_settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.server.ai_settings_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        except OSError as error:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)}); return
        self._get_image_generation_settings()

    def _test_image_generation_settings(self, _payload: Mapping[str, Any]) -> None:
        configuration = _image_generation_configuration(self.server.ai_settings_path)
        if configuration is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Please save the selected image provider configuration first."}); return
        provider, _api_key, base_url, model = configuration
        self._json(HTTPStatus.OK, {"status": "图片服务配置已就绪", "provider": provider, "base_url": base_url, "model": model})

    def _save_serper_settings(self, payload: Mapping[str, Any]) -> None:
        api_key = self._text(payload, "api_key")
        if api_key is None:
            return
        try:
            document = dict(_ai_settings_document(self.server.ai_settings_path))
            integrations = dict(document.get("integrations")) if isinstance(document.get("integrations"), Mapping) else {}
            integrations["serper"] = {"api_key": api_key}
            document["integrations"] = integrations
            self.server.ai_settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.server.ai_settings_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        except OSError as error:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
            return
        self._get_serper_settings()

    def _test_serper_settings(self, payload: Mapping[str, Any]) -> None:
        api_key = self._optional_text(payload, "api_key") or _serper_api_key(self.server.ai_settings_path)
        if api_key is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Please save a Serper API key first."})
            return
        try:
            results = SerperSearchClient(api_key).search(query="apple inc", max_results=1)
        except SerperSearchProtocolError as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {"status": "connected", "provider": "Serper.dev", "website": "https://serper.dev/", "sample_title": results[0]["title"]})

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
            document = dict(_ai_settings_document(self.server.ai_settings_path))
            document.update({"providers": {provider: {"api_key": api_key, "base_url": base_url.rstrip("/"), "model": model}}, "assignments": assignments})
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
        document = dict(_ai_settings_document(self.server.ai_settings_path))
        document.update({"providers": profiles, "assignments": assignments})
        self.server.ai_settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.server.ai_settings_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
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
        research_recovery = payload.get("research_recovery") is True
        failed_title = self._optional_text(payload, "failed_title")
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
                competitor_titles = self._merge_serp_title_memory(connection, project_id, keyword_id, competitor_titles)
                request_data = {"keyword": keyword["keyword"], "locale": locale, "count": count, "search_intent": intent[0] if intent else None, "category": category[0] if category else None, "title_type": title_type, "competitor_titles": competitor_titles, "research_recovery": research_recovery, "failed_title": failed_title}
                if isinstance(self.server.title_generator, OpenAICompatibleTitleGenerator):
                    provider = _ai_assignments(self.server.ai_settings_path)["title_generation"]
                    active_configuration = _provider_configuration(self.server.ai_settings_path, provider)
                    model = active_configuration[2] if active_configuration else None
                else:
                    provider, model = "rule", None
                # Commit the audit row before calling the remote model. A
                # provider failure must not roll the evidence of that failure
                # back with the candidate transaction.
                with connection:
                    cursor = connection.execute(
                        """INSERT INTO title_generation_jobs(project_id,keyword_id,status,request_json,provider,model,requested_count,started_at)
                           VALUES(?,?, 'running', ?,?,?,?, CURRENT_TIMESTAMP)""",
                        (project_id, keyword_id, json.dumps(request_data, ensure_ascii=False), provider, model, count),
                    )
                    job_id = int(cursor.lastrowid)
                try:
                    raw = self.server.title_generator.generate(**request_data)
                    candidates = self._title_candidates_from_response(raw, count)
                except (ValueError, TypeError, TitleGenerationProtocolError, json.JSONDecodeError) as error:
                    summary = (str(error) or "Title generation failed.")[:500]
                    error_code = "provider_error" if isinstance(error, TitleGenerationProtocolError) else "invalid_response"
                    with connection:
                        connection.execute(
                            """UPDATE title_generation_jobs
                               SET status='failed',error_code=?,error_summary=?,completed_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (error_code, summary, job_id),
                        )
                    self._json(HTTPStatus.BAD_GATEWAY, {"error": summary, "job_id": job_id, "provider": provider, "model": model})
                    return
                with connection:
                    for candidate in candidates:
                        self._insert_title_candidate(connection, project_id, keyword_id, candidate, source_type="ai", generation_job_id=job_id)
                    connection.execute("UPDATE title_generation_jobs SET status='succeeded',generated_count=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (len(candidates), job_id))
                row = connection.execute("SELECT * FROM title_generation_jobs WHERE id=?", (job_id,)).fetchone()
        except (sqlite3.Error, ValueError, TypeError) as error:
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
                references = self._merge_serp_title_memory(connection, project_id, keyword_id, references)
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
            with self._database() as connection:
                saved_count = self._persist_serp_title_samples(connection, project_id, keyword_id, locale, titles, "ai")
        except (sqlite3.Error, ValueError, TypeError, TitleGenerationProtocolError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error) or "AI SERP 标题抓取失败。"})
            return
        response: dict[str, Any] = {"keyword": keyword, "titles": titles, "saved_count": saved_count}
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
            with self._database() as connection:
                saved_count = self._persist_serp_title_samples(connection, project_id, keyword_id, locale, titles, "browser")
        except GoogleSerpVerificationRequired as error:
            self._json(HTTPStatus.OK, {"keyword": keyword if "keyword" in locals() else "", "titles": [], "source_type": "browser", "verification_required": True, "verification_image": error.image_base64})
            return
        except (sqlite3.Error, ValueError, TypeError, GoogleSerpProtocolError) as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error) or "浏览器 Google 标题抓取失败。"})
            return
        self._json(HTTPStatus.OK, {"keyword": keyword, "titles": titles, "source_type": "browser", "saved_count": saved_count})

    def _list_serp_title_samples(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        project_values, keyword_values = query.get("project_id", []), query.get("keyword_id", [])
        if len(project_values) != 1 or len(keyword_values) != 1:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "project_id and keyword_id are required."})
            return
        try:
            project_id, keyword_id = int(project_values[0]), int(keyword_values[0])
            with self._database() as connection:
                self._title_keyword(connection, project_id, keyword_id, require_approved=False)
                rows = connection.execute(
                    """SELECT rank,title,source,source_type,locale,captured_at
                       FROM serp_title_samples
                       WHERE project_id=? AND keyword_id=?
                       ORDER BY rank ASC, captured_at DESC, id DESC
                       LIMIT 20""",
                    (project_id, keyword_id),
                ).fetchall()
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {"titles": [dict(row) for row in rows]})

    @staticmethod
    def _persist_serp_title_samples(
        connection: sqlite3.Connection,
        project_id: int,
        keyword_id: int,
        locale: str,
        titles: list[dict[str, str | int | None]],
        source_type: str,
    ) -> int:
        saved = 0
        with connection:
            for position, item in enumerate(titles, start=1):
                title = " ".join(str(item.get("title") or "").split())
                if not title:
                    continue
                rank = item.get("rank")
                rank_value = rank if isinstance(rank, int) and rank > 0 else position
                cursor = connection.execute(
                    """INSERT INTO serp_title_samples(project_id,keyword_id,rank,title,normalized_title,source,source_type,locale)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(project_id,keyword_id,normalized_title) DO UPDATE SET
                         rank=excluded.rank, source=excluded.source, source_type=excluded.source_type,
                         locale=excluded.locale, captured_at=CURRENT_TIMESTAMP""",
                    (project_id, keyword_id, rank_value, title, normalize_keyword(title), item.get("source"), source_type, locale),
                )
                if cursor.rowcount > 0:
                    saved += 1
        return saved

    @staticmethod
    def _merge_serp_title_memory(connection: sqlite3.Connection, project_id: int, keyword_id: int, supplied: list[str]) -> list[str]:
        rows = connection.execute(
            """SELECT title FROM serp_title_samples
               WHERE project_id=? AND keyword_id=?
               ORDER BY rank ASC, captured_at DESC, id DESC LIMIT 20""",
            (project_id, keyword_id),
        ).fetchall()
        merged: list[str] = []
        seen: set[str] = set()
        for title in [*supplied, *(str(row[0]) for row in rows)]:
            clean = " ".join(title.split())
            normalized = normalize_keyword(clean)
            if clean and normalized not in seen:
                seen.add(normalized)
                merged.append(clean)
            if len(merged) >= 20:
                break
        return merged

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
        try:
            deleted = self.server.keyword_repository.soft_delete(
                project_id,
                keyword_ids=raw_ids,
                clear_all=clear_all,
            )
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {"deleted": deleted})

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
        try:
            rows = self.server.keyword_repository.list_keywords(project_id)
        except (sqlite3.Error, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, rows)

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
        if len(parts) != 4 or parts[:2] != ["api", "content-assets"] or parts[3] not in {"briefs", "outlines", "generate", "generate-brief", "generate-outline", "generate-draft", "review-quality", "rewrite-targeted", "preview-competitors", "preview-outline", "preview-content", "restart", "research-competitors", "image-prompts", "generate-images", "prepare-publish", "publish-wordpress"}: return None
        try: return int(parts[2]), parts[3]
        except ValueError: return None

    @staticmethod
    def _content_memory_path(path: str) -> int | None:
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "content-memory"]:
            return None
        try: return int(parts[2])
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
        cited = completed and row["status"] == "ready_to_publish"
        return {
            "outline_status": "completed" if outlined else "pending",
            "outline_status_label": "大纲完成" if outlined else "待大纲",
            "content_status": "completed" if cited else ("needs_sources" if completed else "pending"),
            "content_status_label": "内容完成 · 引用已验证" if cited else ("缺少权威引用 · 不可发布" if completed else "待生成内容"),
        }

    @classmethod
    def _content_asset_payload(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["tags"] = cls._normalise_content_tags(json.loads(value.pop("tags_json", "[]") or "[]"))
        return value | cls._workflow_status_payload(row)

    @classmethod
    def _ensure_content_asset_tags(cls, connection: sqlite3.Connection, row: sqlite3.Row) -> Mapping[str, Any]:
        """Backfill legacy completed articles without charging another AI call."""
        existing = cls._normalise_content_tags(json.loads(str(row["tags_json"] or "[]")))
        if existing or row["current_draft_id"] is None:
            return row
        draft = connection.execute("SELECT markdown FROM content_drafts WHERE id=?", (row["current_draft_id"],)).fetchone()
        if draft is None:
            return row
        brief = connection.execute("SELECT brief_json FROM content_briefs WHERE id=?", (row["current_brief_id"],)).fetchone() if row["current_brief_id"] else None
        brief_json = json.loads(str(brief["brief_json"] or "{}")) if brief else {}
        semantic = brief_json.get("semantic") if isinstance(brief_json, Mapping) else {}
        tags = cls._fallback_content_tags(row, semantic, str(draft["markdown"] or ""))
        with connection:
            connection.execute("UPDATE content_assets SET tags_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(tags, ensure_ascii=False), row["id"]))
        value = dict(row)
        value["tags_json"] = json.dumps(tags, ensure_ascii=False)
        return value

    def _content_asset_detail(self, connection: sqlite3.Connection, project_id: int, asset_id: int) -> dict[str, Any]:
        row = self._ensure_content_asset_tags(connection, self._content_asset(connection, project_id, asset_id))
        payload = self._content_asset_payload(row)
        brief = connection.execute("SELECT * FROM content_briefs WHERE content_asset_id=? AND status='current' ORDER BY id DESC LIMIT 1", (asset_id,)).fetchone()
        outline = connection.execute("SELECT * FROM content_outlines WHERE content_asset_id=? ORDER BY id DESC LIMIT 1", (asset_id,)).fetchone()
        drafts = connection.execute("SELECT * FROM content_drafts WHERE content_asset_id=? ORDER BY version", (asset_id,)).fetchall()
        runs = connection.execute("SELECT * FROM content_generation_runs WHERE content_asset_id=? ORDER BY id", (asset_id,)).fetchall()
        jobs = connection.execute("SELECT * FROM content_generation_jobs WHERE content_asset_id=? ORDER BY id", (asset_id,)).fetchall()
        research = self._latest_competitor_research(connection, asset_id)
        authority_sources = connection.execute(
            """SELECT sources.*, links.section_heading, links.claim_topic, links.created_at AS linked_at
               FROM content_authority_source_links links
               JOIN authority_source_library sources ON sources.id=links.authority_source_id
               WHERE links.project_id=? AND links.content_asset_id=?
               ORDER BY links.id DESC""",
            (project_id, asset_id),
        ).fetchall()
        latest_authority_search = connection.execute("SELECT search_run_id FROM authority_search_results WHERE project_id=? AND content_asset_id=? ORDER BY id DESC LIMIT 1", (project_id, asset_id)).fetchone()
        authority_search_results = connection.execute("SELECT * FROM authority_search_results WHERE project_id=? AND content_asset_id=? AND search_run_id=? ORDER BY id", (project_id, asset_id, latest_authority_search["search_run_id"])).fetchall() if latest_authority_search else []
        current_draft = connection.execute("SELECT * FROM content_drafts WHERE id=?", (row["current_draft_id"],)).fetchone() if row["current_draft_id"] is not None else None
        images = connection.execute("SELECT * FROM content_section_images WHERE project_id=? AND content_asset_id=? AND draft_id=? ORDER BY position", (project_id, asset_id, current_draft["id"])).fetchall() if current_draft else []
        publications = connection.execute("SELECT * FROM content_wordpress_publications WHERE project_id=? AND content_asset_id=? ORDER BY id DESC", (project_id, asset_id)).fetchall()
        learning_memory_rows = connection.execute(
            """SELECT memories.*,links.role,links.relevance_score,links.selected_by_model,links.selected_by_user,links.created_at AS linked_at
               FROM content_memory_links links JOIN content_learning_memories memories ON memories.id=links.memory_id
               WHERE links.content_asset_id=? ORDER BY links.relevance_score DESC,links.id DESC""",
            (asset_id,),
        ).fetchall()
        payload["brief"] = self._content_brief_payload(brief) if brief else None
        payload["outline"] = self._content_outline_payload(connection, outline) if outline else None
        payload["drafts"] = [self._content_draft_payload(draft) for draft in drafts]
        payload["current_draft"] = self._content_draft_payload(current_draft) if current_draft else None
        payload["generation_runs"] = [self._content_run_payload(run) for run in runs]
        payload["generation_jobs"] = [dict(job) for job in jobs]
        payload["competitor_research"] = self._competitor_research_payload(connection, research["id"]) if research else None
        payload["production_readiness"] = self._content_competitor_learning_gate(connection, row)
        payload["authority_sources"] = [self._authority_source_payload(source) for source in authority_sources]
        payload["authority_search_results"] = [dict(result) for result in authority_search_results]
        payload["section_images"] = [dict(image) for image in images]
        payload["wordpress_publications"] = [dict(publication) for publication in publications]
        payload["learning_memories"] = [self._content_learning_memory_payload(memory) for memory in learning_memory_rows]
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
        value["markdown"] = KeywordDiscoveryRequestHandler._sanitize_reader_markdown(str(value.get("markdown") or ""))
        value["sources_used"] = json.loads(value.pop("sources_used_json") or "[]")
        value["unresolved_verify"] = json.loads(value.pop("unresolved_verify_json") or "[]")
        value["qa"] = json.loads(value.pop("qa_json") or "{}")
        return value

    @staticmethod
    def _content_run_payload(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["input"] = json.loads(value.pop("input_json") or "{}")
        workflow_stage = value["input"].get("workflow_stage")
        if workflow_stage in {"industry_rules", "chapter_plan", "company_context_plan", "full_article", "targeted_rewrite"}:
            value["stage"] = workflow_stage
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
    def _database(self) -> Iterator[Any]:
        if self.server.runtime_database.mode is RuntimeDatabaseMode.POSTGRES:
            if self.server.runtime_postgres_factory is None:
                raise RuntimeError("PostgreSQL runtime connection is not configured")
            connection = self.server.runtime_postgres_factory.connect()
        else:
            connection = initialize_database(self.server.database_path)
            connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _credential_database(self) -> Iterator[sqlite3.Connection]:
        # Compatibility bridge for upgrades that still have DPAPI ciphertext
        # in the legacy SQLite credential tables. New writes always go only to
        # the independent vault.
        if str(self.server.database_path) != ":memory:":
            self.server.credential_vault.import_existing(self.server.database_path)
        connection = self.server.credential_vault.connect()
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


def create_server(host: str = "127.0.0.1", port: int = 0, database_path: str | Path = ":memory:", suggest_client: Any | None = None, keyword_reviewer: Any | None = None, title_generator: Any | None = None, serp_title_client: Any | None = None, ai_settings_path: Path | None = None, content_generator: Any | None = None, competitor_content_client: Any | None = None, competitor_search_client: Any | None = None, database_mode: str | None = None, database_url: str | None = None, runtime_state_path: str | Path | None = None, credential_vault_path: str | Path | None = None) -> KeywordDiscoveryServer:
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
    server.competitor_content_client = competitor_content_client or BrowserCompetitorContentClient(server.serp_title_client)
    # Content-page extraction is independent HTTP parsing; only the Google SERP
    # discovery path is delegated to Serper.dev in production.
    server.competitor_search_client = competitor_search_client
    server.collection_service = CollectionService()
    # A file-backed SQLite source must have its schema before legacy encrypted
    # credentials can be imported into the independent local vault.
    if str(database_path) != ":memory:":
        source_bootstrap = initialize_database(server.database_path)
        source_bootstrap.close()
    default_vault_path = (
        Path(credential_vault_path) if credential_vault_path is not None
        else (Path(database_path).parent / "runtime-credentials.sqlite3" if str(database_path) != ":memory:" else Path(os.getenv("TEMP", ".")) / f"seo-runtime-credentials-{uuid.uuid4().hex}.sqlite3")
    )
    server.credential_vault = CredentialVault(default_vault_path, import_database_path=None if str(database_path) == ":memory:" else database_path)
    server.runtime_database = RuntimeDatabaseController(
        server.database_path,
        database_url=database_url,
        state_path=runtime_state_path,
        requested_mode=database_mode,
        credential_vault=server.credential_vault,
    )
    sqlite_projects = SQLiteProjectRepository(server.database_path)
    sqlite_keywords = SQLiteKeywordRepository(server.database_path)
    sqlite_collection = SQLiteCollectionRepository(server.database_path)
    target_url = server.runtime_database.database_url
    server.runtime_postgres_factory = (
        PostgresRuntimeConnectionFactory(server.database_path, target_url) if target_url else None
    )
    postgres_projects = PostgresRuntimeProjectRepository(server.runtime_postgres_factory) if server.runtime_postgres_factory else None
    postgres_keywords = PostgresRuntimeKeywordRepository(server.runtime_postgres_factory) if server.runtime_postgres_factory else None
    postgres_collection = PostgresRuntimeCollectionRepository(server.runtime_postgres_factory) if server.runtime_postgres_factory else None
    server.project_repository = ShadowProjectRepository(sqlite_projects, postgres_projects, server.runtime_database)
    server.keyword_repository = ShadowKeywordRepository(sqlite_keywords, postgres_keywords, server.runtime_database)
    server.collection_repository = ShadowCollectionRepository(sqlite_collection, postgres_collection, server.runtime_database)
    server.gsc_oauth_states = {}
    server.gsc_browser_client = GscBrowserCaptureClient()
    # Apply migrations before the scheduler gets a second SQLite connection.
    # Otherwise a brand-new database can race on schema_migrations at startup.
    if server.runtime_database.mode is RuntimeDatabaseMode.POSTGRES:
        if server.runtime_postgres_factory is None:
            raise RuntimeError("PostgreSQL runtime connection is not configured")
        bootstrap_connection = server.runtime_postgres_factory.connect()
    else:
        bootstrap_connection = initialize_database(server.database_path)
        bootstrap_connection.row_factory = sqlite3.Row
    with bootstrap_connection:
        server.recovered_collection_runs = server.collection_service.recover_interrupted_runs(bootstrap_connection)
        # Ordinary content generation is handled by the HTTP request thread and
        # cannot survive a process restart. Keep the audit row, but never leave
        # an interrupted request looking active forever. Durable full-content
        # Agent jobs are excluded because their generation row intentionally
        # stays open while the workflow waits at approval checkpoints.
        bootstrap_connection.execute(
            """UPDATE content_generation_jobs
               SET status='failed',failed_stage='interrupted',
                   error_summary='Generation was interrupted by a service restart. Retry from the content workspace.',
                   completed_at=CURRENT_TIMESTAMP
               WHERE status='running' AND requested_action<>'full_content_agent'"""
        )
        bootstrap_connection.execute(
            """UPDATE content_generation_runs
               SET status='failed',error_summary='Stage was interrupted by a service restart and superseded by checkpoint recovery.',
                   completed_at=CURRENT_TIMESTAMP
               WHERE status='running'"""
        )
        bootstrap_connection.execute(
            """UPDATE competitor_research_runs
               SET status='failed',error_summary='Competitor research was interrupted by a service restart. Retry this title to start a new traceable collection run.',
                   completed_at=CURRENT_TIMESTAMP
               WHERE status='running'"""
        )
        bootstrap_connection.execute(
            """UPDATE agent_steps
               SET status='failed',error_summary='Step was interrupted by a service restart and recovered from its checkpoint.',
                   completed_at=CURRENT_TIMESTAMP
               WHERE status='running'"""
        )
        bootstrap_connection.execute(
            """UPDATE agent_jobs SET status='queued',updated_at=CURRENT_TIMESTAMP
               WHERE workflow_version='content-agent-v2' AND requested_action='full_content_agent'
                 AND status IN ('running','retrying')"""
        )
        server.recovered_content_agent_jobs = [
            (int(row["project_id"]), int(row["id"]))
            for row in bootstrap_connection.execute(
                """SELECT id,project_id FROM agent_jobs
                   WHERE workflow_version='content-agent-v2' AND requested_action='full_content_agent'
                     AND status IN ('queued','planning') ORDER BY id"""
            ).fetchall()
        ]
        bootstrap_connection.execute(
            """UPDATE agent_jobs SET status='queued',updated_at=CURRENT_TIMESTAMP
               WHERE workflow_version='gsc-feedback-v1' AND requested_action='gsc_feedback_learning'
                 AND status IN ('running','retrying')"""
        )
        server.recovered_gsc_feedback_jobs = [
            (int(row["project_id"]), int(row["id"]))
            for row in bootstrap_connection.execute(
                """SELECT id,project_id FROM agent_jobs
                   WHERE workflow_version='gsc-feedback-v1' AND requested_action='gsc_feedback_learning'
                     AND status IN ('queued','planning') ORDER BY id"""
            ).fetchall()
        ]
    bootstrap_connection.close()
    callback_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    callback_url = f"http://{callback_host}:{server.server_address[1]}"
    server.worker_token = os.getenv("SEO_WORKER_TOKEN") or uuid.uuid4().hex
    server.task_queue = DurableTaskQueue(
        server.database_path,
        callback_url=callback_url,
        worker_token=server.worker_token,
        connection_factory=(
            lambda: server.runtime_postgres_factory.connect()
            if server.runtime_database.mode is RuntimeDatabaseMode.POSTGRES and server.runtime_postgres_factory is not None
            else initialize_database(server.database_path)
        ),
    )
    server.periodic_learning_stop = threading.Event()
    server.periodic_learning_wake = threading.Event()
    server.periodic_learning_thread = None
    return server


def _dispatch_competitor_learning_run(server: KeywordDiscoveryServer, project_id: int, run_id: int) -> None:
    """Run the existing audited HTTP workflow in a background thread.

    Keeping execution behind the same handler means manual and scheduled runs
    share project checks, source filtering, model routing and audit tables.
    """
    host, port = server.server_address[:2]
    try:
        request = Request(
            f"http://{host}:{port}/api/projects/{project_id}/competitor-learning/runs/{run_id}/execute",
            data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=600):
            pass
    except Exception:
        # The execution endpoint persists a detailed error after it starts.
        # If the service is stopping before it can accept the request, keep
        # the queued record for later inspection rather than fabricating one.
        return


def _dispatch_competitor_catalog_collection(server: KeywordDiscoveryServer, project_id: int, run_id: int) -> None:
    """Execute a user-requested catalog collection without blocking the page."""
    host, port = server.server_address[:2]
    for attempt in range(3):
        try:
            request = Request(
                f"http://{host}:{port}/api/competitor-url-catalog/collection-runs/{run_id}/execute",
                data=json.dumps({"project_id": project_id}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=900):
                return
        except Exception:
            # Startup recovery can race the first serve_forever poll. The
            # durable queued run remains safe if all short retries fail.
            if attempt < 2:
                time.sleep(0.1 * (attempt + 1))


def _dispatch_collected_competitor_content_learning(server: KeywordDiscoveryServer, project_id: int, run_id: int) -> None:
    """Run collection-library learning in the background so the UI stays responsive."""
    host, port = server.server_address[:2]
    try:
        request = Request(
            f"http://{host}:{port}/api/competitor-content-learning/runs/{run_id}/execute",
            data=json.dumps({"project_id": project_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=900):
            pass
    except Exception:
        # The durable run stays visible; the endpoint itself records failures
        # after execution begins, without hiding an upstream model error.
        return


def _dispatch_content_agent_job(server: KeywordDiscoveryServer, project_id: int, job_id: int) -> None:
    """Advance one durable content Agent job through its audited HTTP route."""

    host, port = server.server_address[:2]
    for attempt in range(20):
        try:
            request = Request(
                f"http://{host}:{port}/api/agent-jobs/{job_id}/execute",
                data=json.dumps({"project_id": project_id}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=1800):
                return
        except Exception:
            if attempt < 19:
                time.sleep(min(1.0, 0.1 * (attempt + 1)))


def _dispatch_gsc_feedback_job(server: KeywordDiscoveryServer, project_id: int, job_id: int) -> None:
    """Resume one durable GSC feedback job through the shared Agent route."""

    _dispatch_content_agent_job(server, project_id, job_id)


def _periodic_competitor_learning_loop(server: KeywordDiscoveryServer) -> None:
    """Queue due project schedules without blocking page navigation or requests."""
    while not server.periodic_learning_stop.is_set():
        try:
            server.task_queue.reclaim_expired_leases()
            if server.runtime_database.mode is RuntimeDatabaseMode.POSTGRES:
                if server.runtime_postgres_factory is None:
                    raise RuntimeError("PostgreSQL runtime connection is not configured")
                connection = server.runtime_postgres_factory.connect()
            else:
                connection = initialize_database(server.database_path)
                connection.row_factory = sqlite3.Row
            with connection:
                schedules = connection.execute(
                    """SELECT * FROM competitor_learning_schedules
                       WHERE enabled=1 AND next_run_at IS NOT NULL AND datetime(next_run_at)<=datetime('now')"""
                ).fetchall()
                queued: list[tuple[int, int]] = []
                for schedule in schedules:
                    duplicate = connection.execute(
                        "SELECT 1 FROM competitor_learning_runs WHERE schedule_id=? AND status IN ('queued','running')",
                        (schedule["id"],),
                    ).fetchone()
                    if duplicate is not None:
                        continue
                    try: topics = json.loads(schedule["topics_json"] or "[]")
                    except (TypeError, json.JSONDecodeError): topics = []
                    topic = next((item for item in topics if isinstance(item, str) and item.strip()), None)
                    if not topic:
                        connection.execute("UPDATE competitor_learning_schedules SET enabled=0,next_run_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (schedule["id"],))
                        continue
                    cursor = connection.execute("INSERT INTO competitor_learning_runs(project_id,schedule_id,topic,trigger_type) VALUES(?,?,?, 'scheduled')", (schedule["project_id"], schedule["id"], topic[:300]))
                    connection.execute("UPDATE competitor_learning_schedules SET next_run_at=datetime('now','+' || interval_days || ' days'),updated_at=CURRENT_TIMESTAMP WHERE id=?", (schedule["id"],))
                    queued.append((int(schedule["project_id"]), int(cursor.lastrowid)))
            connection.close()
            for project_id, run_id in queued:
                server.enqueue_background_task("competitor_learning", project_id, run_id)
        except Exception:
            # The next cycle retries database availability.  Task rows, once
            # created, remain visible rather than disappearing with the loop.
            pass
        server.periodic_learning_wake.wait(60)
        server.periodic_learning_wake.clear()


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


def _image_generation_settings(path: Path = AI_SETTINGS_FILE) -> Mapping[str, Any]:
    document = _ai_settings_document(path)
    integrations = document.get("integrations")
    image = integrations.get("image_generation") if isinstance(integrations, Mapping) else None
    return image if isinstance(image, Mapping) else {}


def _image_generation_provider(path: Path = AI_SETTINGS_FILE) -> str:
    provider = _image_generation_settings(path).get("provider")
    return provider if provider in IMAGE_GENERATION_PROVIDERS else "openai"


def _image_generation_model(path: Path = AI_SETTINGS_FILE) -> str:
    image = _image_generation_settings(path)
    model = image.get("model") if isinstance(image, Mapping) else None
    default = "Kwai-Kolors/Kolors" if _image_generation_provider(path) == "siliconflow" else "gpt-image-2"
    return model.strip() if isinstance(model, str) and model.strip() else default


def _image_generation_configuration(path: Path = AI_SETTINGS_FILE) -> tuple[str, str, str, str] | None:
    provider = _image_generation_provider(path)
    model = _image_generation_model(path)
    if provider == "siliconflow":
        api_key = _image_generation_settings(path).get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            return provider, api_key.strip(), SILICONFLOW_IMAGE_BASE_URL, model
        return None
    configuration = _provider_configuration(path, "openai")
    if configuration is None:
        return None
    api_key, base_url, _text_model = configuration
    return provider, api_key, base_url, model


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


def _gsc_settings(path: Path) -> dict[str, str]:
    integrations = _ai_settings_document(path).get("integrations")
    configuration = integrations.get("gsc") if isinstance(integrations, Mapping) else {}
    return {
        "client_id": str(configuration.get("client_id") or "") if isinstance(configuration, Mapping) else "",
        "client_secret": str(configuration.get("client_secret") or "") if isinstance(configuration, Mapping) else "",
    }


def _serper_api_key(path: Path) -> str | None:
    integrations = _ai_settings_document(path).get("integrations")
    raw = integrations.get("serper") if isinstance(integrations, Mapping) else None
    value = raw.get("api_key") if isinstance(raw, Mapping) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


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
