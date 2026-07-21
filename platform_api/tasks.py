"""Celery task entry points for the visible P0/P1 lifecycle."""

from __future__ import annotations

from platform_api.celery_app import celery_app
from platform_api.storage import assess_sources, initialize_platform_schema, initialize_task, record_task_failure, run_legacy_import


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
