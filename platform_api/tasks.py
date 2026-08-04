"""Celery task entry points for the visible P0/P1 lifecycle."""

from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

from platform_api.celery_app import celery_app
from platform_api.site_crawler import crawl_site
from platform_api.storage import (
    assess_sources,
    complete_knowledge_crawl_run,
    connection,
    fail_knowledge_crawl_run,
    initialize_platform_schema,
    initialize_task,
    record_task_failure,
    run_legacy_import,
)


@celery_app.task(
    bind=True,
    name="platform.execute_workspace_queue_job",
    autoretry_for=(OSError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_kwargs={"max_retries": 8},
)
def execute_workspace_queue_job(self, queue_job_id: int, callback_url: str) -> dict[str, object]:
    """Deliver one durable workspace task without reading its SQLite data."""
    token = os.getenv("SEO_WORKER_TOKEN", "")
    if not token:
        raise RuntimeError("SEO_WORKER_TOKEN is required by the workspace worker")
    request = Request(
        f"{callback_url.rstrip('/')}/api/internal/task-queue/{queue_job_id}/execute",
        data=b"{}",
        headers={"Content-Type": "application/json", "X-SEO-Worker-Token": token},
        method="POST",
    )
    with urlopen(request, timeout=1900) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("status") == "retry_wait":
        raise self.retry(countdown=300)
    return result


@celery_app.task(name="platform.initialize_content_task")
def initialize_content_task(task_id: int) -> dict[str, int | str]:
    initialize_platform_schema()
    try:
        initialize_task(task_id)
    except Exception as error:
        record_task_failure(task_id, "task_initialization", error)
        raise
    return {"task_id": task_id, "status": "waiting_for_sources"}


@celery_app.task(name="platform.assess_task_sources")
def assess_task_sources(task_id: int) -> dict[str, object]:
    initialize_platform_schema()
    try:
        return assess_sources(task_id)
    except Exception as error:
        record_task_failure(task_id, "source_assessment", error)
        raise


@celery_app.task(name="platform.run_legacy_import")
def run_legacy_import_task(legacy_project_id: int) -> dict[str, object]:
    initialize_platform_schema()
    return run_legacy_import(legacy_project_id)


@celery_app.task(name="platform.crawl_website_knowledge")
def crawl_website_knowledge_task(run_id: int) -> dict[str, object]:
    """Run a full same-site knowledge crawl outside of the browser request."""
    initialize_platform_schema()
    try:
        with connection() as database, database.cursor() as cursor:
            cursor.execute(
                """SELECT runs.site_id,runs.max_pages,websites.domain FROM site_knowledge_crawl_runs runs
                   JOIN websites ON websites.id=runs.site_id WHERE runs.id=%s FOR UPDATE""",
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("knowledge crawl run does not exist")
            cursor.execute("UPDATE site_knowledge_crawl_runs SET status='running',message='Discovering and classifying relevant company pages.' WHERE id=%s", (run_id,))
        pages = crawl_site(str(row["domain"]), int(row["max_pages"]))
        return complete_knowledge_crawl_run(run_id, pages)
    except Exception as error:
        fail_knowledge_crawl_run(run_id, error)
        raise
