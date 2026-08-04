"""SQLite-backed task queue with local-worker and optional Celery dispatch.

The queue owns only delivery.  Business state, checkpoints and tool audits stay
in their existing project-scoped tables and execute through the same protected
HTTP routes used by manual runs.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Mapping
from urllib.request import Request, urlopen

from seo_control.infrastructure.database import initialize_database


TASK_TYPES = frozenset({
    "competitor_catalog_collection",
    "collected_competitor_learning",
    "competitor_learning",
    "agent_job",
})
ACTIVE_STATUSES = ("queued", "running", "retry_wait")


class QueueConfigurationError(ValueError):
    """Raised when an optional queue backend is selected but not configured."""


@dataclass(frozen=True)
class QueueJob:
    id: int
    project_id: int
    task_type: str
    resource_id: int
    status: str
    attempt_count: int
    max_attempts: int
    dedup_key: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "QueueJob":
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})


class DurableTaskQueue:
    """Persist delivery before dispatching work to a bounded execution backend."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        callback_url: str,
        worker_token: str,
        backend: str | None = None,
        concurrency: int | None = None,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        selected = (backend or os.getenv("SEO_TASK_QUEUE_BACKEND", "local")).strip().casefold()
        if selected not in {"local", "celery"}:
            raise QueueConfigurationError("SEO_TASK_QUEUE_BACKEND must be local or celery")
        if selected == "celery" and not os.getenv("SEO_WORKER_TOKEN"):
            raise QueueConfigurationError("SEO_WORKER_TOKEN is required when Celery is enabled")
        self.database_path = database_path
        self.connection_factory = connection_factory
        self.callback_url = callback_url.rstrip("/")
        self.worker_token = worker_token
        self.backend = selected
        raw_concurrency = concurrency if concurrency is not None else int(os.getenv("SEO_LOCAL_WORKER_CONCURRENCY", "2"))
        self.concurrency = max(1, min(int(raw_concurrency), 8))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._inflight: set[int] = set()
        self._inflight_lock = threading.Lock()

    def start(self) -> None:
        """Recover interrupted deliveries and start the local polling worker."""
        self.recover_interrupted()
        if self.backend == "celery":
            self._redispatch_celery_due()
            return
        if self._thread is not None:
            return
        self._executor = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="seo-queue")
        self._thread = threading.Thread(target=self._local_loop, daemon=True, name="durable-task-queue")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        if self._executor is not None:
            # The HTTP server is still alive while shutdown() runs, so claimed
            # callbacks can finish and release SQLite handles cleanly. Queued
            # but not started callbacks are cancelled and remain recoverable.
            self._executor.shutdown(wait=True, cancel_futures=True)

    def enqueue(
        self,
        *,
        project_id: int,
        task_type: str,
        resource_id: int,
        payload: Mapping[str, Any] | None = None,
        max_attempts: int = 3,
        dedup_key: str | None = None,
    ) -> dict[str, Any]:
        if task_type not in TASK_TYPES:
            raise ValueError(f"unsupported durable task type: {task_type}")
        if project_id < 1 or resource_id < 1:
            raise ValueError("project_id and resource_id must be positive")
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be from 1 to 10")
        stable_key = dedup_key or f"{task_type}:{project_id}:{resource_id}"
        connection = self._database()
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                    raise ValueError("project does not exist")
                existing = connection.execute(
                    "SELECT * FROM durable_task_queue WHERE dedup_key=? AND status IN ('queued','running','retry_wait') ORDER BY id DESC LIMIT 1",
                    (stable_key,),
                ).fetchone()
                if existing is not None:
                    row = existing
                else:
                    cursor = connection.execute(
                        """INSERT INTO durable_task_queue(
                               project_id,task_type,resource_id,payload_json,dedup_key,status,max_attempts,available_at
                           ) VALUES(?,?,?,?,?,'queued',?,CURRENT_TIMESTAMP)""",
                        (project_id, task_type, resource_id, json.dumps(dict(payload or {}), ensure_ascii=False), stable_key[:300], max_attempts),
                    )
                    row = connection.execute("SELECT * FROM durable_task_queue WHERE id=?", (cursor.lastrowid,)).fetchone()
        finally:
            connection.close()
        value = dict(row)
        if self.backend == "celery" and not value.get("celery_task_id"):
            celery_id = self._send_celery(int(value["id"]))
            with self._connection() as connection, connection:
                connection.execute(
                    "UPDATE durable_task_queue SET celery_task_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (celery_id, value["id"]),
                )
            value["celery_task_id"] = celery_id
        else:
            self._wake.set()
        return value

    def recover_interrupted(self) -> int:
        """Return abandoned leases to retry_wait without losing attempts."""
        with self._connection() as connection, connection:
            cursor = connection.execute(
                """UPDATE durable_task_queue
                   SET status='retry_wait',available_at=CURRENT_TIMESTAMP,lease_expires_at=NULL,
                       celery_task_id=NULL,
                       last_error=CASE WHEN trim(last_error)='' THEN 'Worker stopped before delivery completed.' ELSE last_error END,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE status='running'"""
            )
            connection.execute(
                "UPDATE durable_task_queue SET status='queued',updated_at=CURRENT_TIMESTAMP WHERE status='retry_wait' AND datetime(available_at)<=datetime('now')"
            )
            return int(cursor.rowcount)

    def reclaim_expired_leases(self) -> int:
        """Requeue workers that vanished after claiming a delivery."""
        with self._connection() as connection, connection:
            cursor = connection.execute(
                """UPDATE durable_task_queue
                   SET status=CASE WHEN attempt_count>=max_attempts THEN 'failed' ELSE 'retry_wait' END,
                       available_at=CURRENT_TIMESTAMP,lease_expires_at=NULL,celery_task_id=NULL,
                       completed_at=CASE WHEN attempt_count>=max_attempts THEN CURRENT_TIMESTAMP ELSE NULL END,
                       last_error='Worker lease expired before delivery completed.',updated_at=CURRENT_TIMESTAMP
                   WHERE status='running' AND lease_expires_at IS NOT NULL
                     AND datetime(lease_expires_at)<=datetime('now')"""
            )
            return int(cursor.rowcount)

    def _redispatch_celery_due(self) -> None:
        connection = self._database()
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """SELECT id FROM durable_task_queue
                   WHERE status IN ('queued','retry_wait') AND datetime(available_at)<=datetime('now')
                     AND (celery_task_id IS NULL OR trim(celery_task_id)='')
                   ORDER BY id LIMIT 500"""
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            task_id = self._send_celery(int(row["id"]))
            with self._connection() as update, update:
                update.execute(
                    "UPDATE durable_task_queue SET celery_task_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (task_id, int(row["id"])),
                )

    def due_job_ids(self, limit: int | None = None) -> list[int]:
        maximum = limit or self.concurrency
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id FROM durable_task_queue
                   WHERE status IN ('queued','retry_wait') AND datetime(available_at)<=datetime('now')
                   ORDER BY available_at,id LIMIT ?""",
                (maximum,),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def claim(self, job_id: int) -> dict[str, Any] | None:
        connection = self._database()
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                cursor = connection.execute(
                    """UPDATE durable_task_queue
                       SET status='running',attempt_count=attempt_count+1,started_at=COALESCE(started_at,CURRENT_TIMESTAMP),
                           lease_expires_at=datetime('now','+35 minutes'),updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('queued','retry_wait') AND datetime(available_at)<=datetime('now')""",
                    (job_id,),
                )
                if cursor.rowcount != 1:
                    return None
                row = connection.execute("SELECT * FROM durable_task_queue WHERE id=?", (job_id,)).fetchone()
                return dict(row)
        finally:
            connection.close()

    def complete(self, job_id: int) -> dict[str, Any]:
        return self._finish(job_id, status="completed", error="")

    def fail(self, job_id: int, error: Exception) -> dict[str, Any]:
        connection = self._database()
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                row = connection.execute("SELECT * FROM durable_task_queue WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise ValueError("queue job does not exist")
                terminal = int(row["attempt_count"]) >= int(row["max_attempts"])
                status = "failed" if terminal else "retry_wait"
                delay_minutes = min(45, 5 * (3 ** max(0, int(row["attempt_count"]) - 1)))
                connection.execute(
                    """UPDATE durable_task_queue SET status=?,last_error=?,lease_expires_at=NULL,
                       available_at=CASE WHEN ?='failed' THEN available_at ELSE datetime('now','+' || ? || ' minutes') END,
                       completed_at=CASE WHEN ?='failed' THEN CURRENT_TIMESTAMP ELSE NULL END,updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (status, str(error)[:1200], status, delay_minutes, status, job_id),
                )
                result = connection.execute("SELECT * FROM durable_task_queue WHERE id=?", (job_id,)).fetchone()
                return dict(result)
        finally:
            connection.close()

    def get(self, job_id: int) -> dict[str, Any]:
        connection = self._database()
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute("SELECT * FROM durable_task_queue WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("queue job does not exist")
            return dict(row)
        finally:
            connection.close()

    def _finish(self, job_id: int, *, status: str, error: str) -> dict[str, Any]:
        connection = self._database()
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                connection.execute(
                    """UPDATE durable_task_queue SET status=?,last_error=?,lease_expires_at=NULL,
                       completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (status, error[:1200], job_id),
                )
                row = connection.execute("SELECT * FROM durable_task_queue WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise ValueError("queue job does not exist")
                return dict(row)
        finally:
            connection.close()

    def _database(self) -> Any:
        if self.connection_factory is not None:
            return self.connection_factory()
        return initialize_database(self.database_path)

    @contextmanager
    def _connection(self):
        connection = self._database()
        try:
            yield connection
        finally:
            connection.close()

    def _local_loop(self) -> None:
        while not self._stop.is_set():
            self.reclaim_expired_leases()
            for job_id in self.due_job_ids(limit=self.concurrency * 2):
                with self._inflight_lock:
                    if job_id in self._inflight or len(self._inflight) >= self.concurrency:
                        continue
                    self._inflight.add(job_id)
                assert self._executor is not None
                self._executor.submit(self._deliver_locally, job_id)
            self._wake.wait(1.0)
            self._wake.clear()

    def _deliver_locally(self, job_id: int) -> None:
        try:
            request = Request(
                f"{self.callback_url}/api/internal/task-queue/{job_id}/execute",
                data=b"{}",
                headers={"Content-Type": "application/json", "X-SEO-Worker-Token": self.worker_token},
                method="POST",
            )
            with urlopen(request, timeout=1900):
                pass
        finally:
            with self._inflight_lock:
                self._inflight.discard(job_id)
            self._wake.set()

    def _send_celery(self, job_id: int) -> str:
        try:
            from celery import Celery
        except ImportError as error:  # pragma: no cover - optional production dependency
            raise QueueConfigurationError("install requirements-queue.txt before enabling Celery") from error
        broker = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        callback = os.getenv("SEO_WORKSPACE_CALLBACK_URL", self.callback_url).rstrip("/")
        app = Celery("seo_control_dispatch", broker=broker, backend=broker)
        result = app.send_task("platform.execute_workspace_queue_job", args=[job_id, callback], queue="workspace")
        return str(result.id)
