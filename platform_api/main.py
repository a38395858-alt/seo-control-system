"""P0/P1 FastAPI control plane for the B2B SEO Agent platform."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from redis import Redis

from platform_api import storage
from platform_api.tasks import assess_task_sources, initialize_content_task, run_legacy_import_task


class WebsiteCreate(BaseModel):
    domain: str = Field(min_length=3, max_length=255)
    industry: str = Field(min_length=2, max_length=160)
    audience: str = Field(default="", max_length=300)
    brand_tone: str = Field(default="", max_length=300)
    country_code: str = Field(default="US", max_length=8)
    language_code: str = Field(default="en-US", max_length=16)
    product_scope: str = Field(default="", max_length=1000)
    prohibited_claims: list[str] = Field(default_factory=list)
    is_legacy: bool = False
    domain_status: str = "verified"
    workspace_project_id: int | None = None


class WebsiteUpdate(BaseModel):
    domain: str | None = Field(default=None, min_length=3, max_length=255)
    industry: str | None = Field(default=None, min_length=2, max_length=160)
    audience: str | None = Field(default=None, max_length=300)
    brand_tone: str | None = Field(default=None, max_length=300)
    country_code: str | None = Field(default=None, max_length=8)
    language_code: str | None = Field(default=None, max_length=16)
    product_scope: str | None = Field(default=None, max_length=1000)
    prohibited_claims: list[str] | None = None
    domain_status: str | None = Field(default=None, max_length=32)
    workspace_project_id: int | None = None


class KnowledgeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    source_type: str = Field(pattern="^(upload|domain|note)$")
    url: str = Field(default="", max_length=2000)
    content: str = Field(default="", max_length=100000)


class SiteTermCreate(BaseModel):
    term: str = Field(min_length=1, max_length=255)
    kind: str = Field(default="term", max_length=40)
    definition: str = Field(default="", max_length=3000)
    source_note: str = Field(default="", max_length=1000)


class SiteFactCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    kind: str = Field(default="fact", max_length=40)
    detail: str = Field(min_length=1, max_length=6000)
    source_note: str = Field(default="", max_length=1000)


class TaskCreate(BaseModel):
    website_id: int
    target_keyword: str = Field(min_length=2, max_length=300)
    title: str = Field(default="", max_length=500)
    task_type: str = Field(default="content", max_length=40)


class SourceCreate(BaseModel):
    source_type: str = Field(pattern="^(url|note|document)$")
    label: str = Field(min_length=1, max_length=300)
    url: str = Field(default="", max_length=2000)
    publisher: str = Field(default="", max_length=300)
    published_at: str = Field(default="", max_length=50)
    availability: str = Field(default="available", pattern="^(available|unavailable)$")
    content: str = Field(default="", max_length=50000)


class OutlineSection(BaseModel):
    heading: str = Field(min_length=1, max_length=500)
    reader_question: str = Field(min_length=1, max_length=1000)
    purpose: str = Field(min_length=1, max_length=1000)
    key_points: list[str] = Field(default_factory=list)
    source_item_ids: list[int] = Field(min_length=1)
    format: str = Field(pattern="^(paragraphs|list|table)$")
    title_promise: str = Field(default="", max_length=100)


class OutlineUpdate(BaseModel):
    sections: list[OutlineSection] = Field(min_length=5, max_length=8)


@asynccontextmanager
async def lifespan(_: FastAPI):
    storage.initialize_platform_schema()
    yield


app = FastAPI(title="B2B SEO Agent Platform API", version="0.3.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])


@app.get("/", include_in_schema=False)
def platform_home() -> RedirectResponse:
    """Keep the API port from looking like a broken website when opened directly."""
    return RedirectResponse(url="http://127.0.0.1:8000/projects", status_code=307)


def _database_ready() -> bool:
    with storage.connection() as database, database.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return True


def _redis_ready() -> bool:
    return bool(Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=3).ping())


def _translate(error: Exception) -> HTTPException:
    return HTTPException(status_code=404 if "does not exist" in str(error) else 400, detail=str(error) or "platform request failed")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": os.getenv("APP_ENV", "development")}


@app.get("/ready")
def ready() -> dict[str, str]:
    try:
        if not _database_ready() or not _redis_ready():
            raise RuntimeError("dependency returned an unexpected response")
    except Exception as error:
        raise HTTPException(status_code=503, detail="platform dependencies are not ready") from error
    return {"status": "ready", "database": "postgresql", "queue": "redis"}


@app.get("/api/dashboard")
def dashboard() -> dict[str, object]:
    try:
        return {"services": {"api": "ready", "database": "ready" if _database_ready() else "unavailable", "queue": "ready" if _redis_ready() else "unavailable", "orchestrator": "celery workflow"}, "summary": storage.dashboard_summary()}
    except Exception as error:
        raise HTTPException(status_code=503, detail="platform dashboard is unavailable") from error


@app.get("/api/websites")
def websites() -> list[dict[str, Any]]:
    return storage.list_websites()


@app.post("/api/websites", status_code=201)
def add_website(payload: WebsiteCreate) -> dict[str, Any]:
    try:
        value = payload.model_dump()
        value["domain"] = value["domain"].strip().lower()
        value["industry"] = value["industry"].strip()
        return storage.create_website(**value)
    except Exception as error:
        raise _translate(error) from error


@app.patch("/api/websites/{site_id}")
def edit_website(site_id: int, payload: WebsiteUpdate) -> dict[str, Any]:
    try:
        return storage.update_website(site_id, payload.model_dump(exclude_none=True))
    except Exception as error:
        raise _translate(error) from error


@app.delete("/api/websites/{site_id}")
def remove_website(site_id: int) -> None:
    try:
        storage.delete_website(site_id)
    except Exception as error:
        raise _translate(error) from error


@app.get("/api/websites/{site_id}/knowledge")
def knowledge(site_id: int) -> list[dict[str, Any]]:
    try:
        return storage.list_knowledge(site_id)
    except Exception as error:
        raise _translate(error) from error


@app.post("/api/websites/{site_id}/knowledge", status_code=201)
def add_knowledge(site_id: int, payload: KnowledgeCreate) -> dict[str, Any]:
    try:
        return storage.add_knowledge(site_id, payload.model_dump())
    except Exception as error:
        raise _translate(error) from error


@app.delete("/api/websites/{site_id}/knowledge/{document_id}")
def remove_knowledge(site_id: int, document_id: int) -> None:
    try:
        storage.delete_knowledge(site_id, document_id)
    except Exception as error:
        raise _translate(error) from error


@app.get("/api/websites/{site_id}/knowledge/context")
def get_knowledge_context(site_id: int) -> dict[str, Any]:
    try:
        return storage.knowledge_context(site_id)
    except Exception as error:
        raise _translate(error) from error


@app.get("/api/websites/{site_id}/terms")
def terms(site_id: int) -> list[dict[str, Any]]:
    try:
        return storage.list_site_items(site_id, "site_terms")
    except Exception as error:
        raise _translate(error) from error


@app.post("/api/websites/{site_id}/terms", status_code=201)
def add_term(site_id: int, payload: SiteTermCreate) -> dict[str, Any]:
    try:
        return storage.create_site_item(site_id, "site_terms", payload.model_dump())
    except Exception as error:
        raise _translate(error) from error


@app.delete("/api/websites/{site_id}/terms/{item_id}")
def remove_term(site_id: int, item_id: int) -> None:
    try:
        storage.delete_site_item(site_id, "site_terms", item_id)
    except Exception as error:
        raise _translate(error) from error


@app.get("/api/websites/{site_id}/facts")
def facts(site_id: int) -> list[dict[str, Any]]:
    try:
        return storage.list_site_items(site_id, "site_facts")
    except Exception as error:
        raise _translate(error) from error


@app.post("/api/websites/{site_id}/facts", status_code=201)
def add_fact(site_id: int, payload: SiteFactCreate) -> dict[str, Any]:
    try:
        return storage.create_site_item(site_id, "site_facts", payload.model_dump())
    except Exception as error:
        raise _translate(error) from error


@app.delete("/api/websites/{site_id}/facts/{item_id}")
def remove_fact(site_id: int, item_id: int) -> None:
    try:
        storage.delete_site_item(site_id, "site_facts", item_id)
    except Exception as error:
        raise _translate(error) from error


@app.get("/api/tasks")
def tasks() -> list[dict[str, Any]]:
    return storage.list_tasks()


@app.post("/api/tasks", status_code=202)
def add_task(payload: TaskCreate) -> dict[str, Any]:
    try:
        task = storage.create_task(**payload.model_dump())
        result = initialize_content_task.delay(int(task["id"]))
        storage.set_task_celery_id(int(task["id"]), result.id)
        task["celery_task_id"] = result.id
        return task
    except Exception as error:
        raise _translate(error) from error


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: int, site_id: int | None = Query(default=None)) -> dict[str, Any]:
    try:
        return storage.get_task_detail(task_id, site_id)
    except Exception as error:
        raise _translate(error) from error


@app.get("/api/tasks/{task_id}/sources")
def sources(task_id: int, site_id: int | None = Query(default=None)) -> list[dict[str, Any]]:
    try:
        return storage.list_sources(task_id, site_id)
    except Exception as error:
        raise _translate(error) from error


@app.post("/api/tasks/{task_id}/sources", status_code=201)
def add_source(task_id: int, payload: SourceCreate, site_id: int | None = Query(default=None)) -> dict[str, Any]:
    try:
        return storage.add_source(task_id, payload.model_dump(), site_id)
    except Exception as error:
        raise _translate(error) from error


@app.delete("/api/tasks/{task_id}/sources/{item_id}")
def remove_source(task_id: int, item_id: int, site_id: int | None = Query(default=None)) -> None:
    try:
        storage.delete_source(task_id, item_id, site_id)
    except Exception as error:
        raise _translate(error) from error


@app.post("/api/tasks/{task_id}/assess-sources", status_code=202)
def assess_sources(task_id: int, site_id: int | None = Query(default=None)) -> dict[str, str]:
    try:
        if site_id is not None:
            storage.get_task_detail(task_id, site_id)
        result = assess_task_sources.delay(task_id)
        return {"status": "queued", "celery_task_id": result.id}
    except Exception as error:
        raise _translate(error) from error


@app.get("/api/tasks/{task_id}/outline")
def outline(task_id: int, site_id: int | None = Query(default=None)) -> dict[str, Any]:
    try:
        return storage.get_outline(task_id, site_id)
    except Exception as error:
        raise _translate(error) from error


@app.put("/api/tasks/{task_id}/outline")
def save_outline(task_id: int, payload: OutlineUpdate, site_id: int | None = Query(default=None)) -> dict[str, Any]:
    try:
        return storage.save_outline(task_id, [section.model_dump() for section in payload.sections], site_id)
    except Exception as error:
        raise _translate(error) from error


@app.post("/api/tasks/{task_id}/outline/confirm")
def confirm_outline(task_id: int, site_id: int | None = Query(default=None)) -> dict[str, Any]:
    try:
        return storage.confirm_outline(task_id, site_id)
    except Exception as error:
        raise _translate(error) from error


@app.post("/api/legacy-imports/preview")
def legacy_preview() -> dict[str, Any]:
    try:
        return storage.preview_legacy_import()
    except Exception as error:
        raise _translate(error) from error


@app.post("/api/legacy-imports/run", status_code=202)
def legacy_run() -> dict[str, Any]:
    try:
        queued = storage.queue_legacy_import_runs()
        for run in queued:
            if run["status"] != "completed":
                run_legacy_import_task.delay(int(run["legacy_project_id"]))
        return {"status": "queued", "runs": queued}
    except Exception as error:
        raise _translate(error) from error


@app.get("/api/legacy-imports")
def legacy_imports() -> list[dict[str, Any]]:
    return storage.list_legacy_imports()
