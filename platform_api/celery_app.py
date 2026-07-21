"""Celery bootstrap kept intentionally small until task modules are introduced."""

from __future__ import annotations

import os

from celery import Celery


celery_app = Celery("seo_agent_platform")
celery_app.conf.update(
    broker_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
    result_backend=os.getenv("REDIS_URL", "redis://redis:6379/0"),
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    task_track_started=True,
    imports=("platform_api.tasks",),
)
