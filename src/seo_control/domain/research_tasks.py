"""Domain model for keyword-research jobs.

This module intentionally contains no persistence, HTTP, or background-worker
concerns.  It protects the task lifecycle and validates the source-specific
parameters before an application service schedules any work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class TaskValidationError(ValueError):
    """Raised when a research task has an unsupported source or invalid input."""


class InvalidTaskTransitionError(ValueError):
    """Raised when a task is moved outside its permitted lifecycle."""


class TaskSource(str, Enum):
    """Approved sources for the first keyword-mining release."""

    GOOGLE_ADS = "google_ads"
    GOOGLE_SUGGEST = "google_suggest"
    FILE_IMPORT = "file_import"


class TaskStatus(str, Enum):
    """Lifecycle states for a keyword-research task."""

    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    PARSING = "parsing"
    AWAITING_IMPORT = "awaiting_import"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED_RATE_LIMIT = "paused_rate_limit"


_ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.DRAFT: {TaskStatus.QUEUED},
    TaskStatus.QUEUED: {TaskStatus.RUNNING},
    TaskStatus.RUNNING: {
        TaskStatus.PARSING,
        TaskStatus.FAILED,
        TaskStatus.PAUSED_RATE_LIMIT,
    },
    TaskStatus.PARSING: {
        TaskStatus.AWAITING_IMPORT,
        TaskStatus.FAILED,
        TaskStatus.PAUSED_RATE_LIMIT,
    },
    TaskStatus.AWAITING_IMPORT: {TaskStatus.COMPLETED, TaskStatus.FAILED},
    TaskStatus.PAUSED_RATE_LIMIT: {TaskStatus.QUEUED, TaskStatus.FAILED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
}


@dataclass
class ResearchTask:
    """A validated task submitted to one keyword-mining source."""

    source: TaskSource
    parameters: dict[str, Any]
    status: TaskStatus = TaskStatus.DRAFT
    failure_reason: str | None = None
    retry_after_seconds: int | None = None

    @classmethod
    def create(
        cls,
        *,
        source: TaskSource | str,
        parameters: Mapping[str, Any] | None,
    ) -> "ResearchTask":
        """Create a draft task after validating its source-specific inputs."""

        try:
            normalized_source = source if isinstance(source, TaskSource) else TaskSource(source)
        except (TypeError, ValueError) as error:
            raise TaskValidationError(f"Unsupported keyword source: {source!r}") from error

        normalized_parameters = dict(parameters or {})
        cls._validate_parameters(normalized_source, normalized_parameters)
        return cls(source=normalized_source, parameters=normalized_parameters)

    @staticmethod
    def _validate_parameters(source: TaskSource, parameters: Mapping[str, Any]) -> None:
        if source is TaskSource.GOOGLE_SUGGEST:
            seed_keywords = parameters.get("seed_keywords")
            if not isinstance(seed_keywords, (list, tuple)) or not any(
                isinstance(keyword, str) and keyword.strip() for keyword in seed_keywords
            ):
                raise TaskValidationError(
                    "Google Suggest tasks require at least one non-empty seed keyword."
                )
            ResearchTask._require_non_empty_string(parameters, "hl", "Google Suggest")
            ResearchTask._require_non_empty_string(parameters, "gl", "Google Suggest")

        if source is TaskSource.GOOGLE_ADS:
            ResearchTask._require_non_empty_string(parameters, "language", "Google Ads")
            geographic_targets = parameters.get("geo_target_ids")
            if not isinstance(geographic_targets, (list, tuple)) or not geographic_targets:
                raise TaskValidationError(
                    "Google Ads tasks require at least one geographic target."
                )

    @staticmethod
    def _require_non_empty_string(
        parameters: Mapping[str, Any], key: str, source_label: str
    ) -> None:
        value = parameters.get(key)
        if not isinstance(value, str) or not value.strip():
            raise TaskValidationError(f"{source_label} tasks require a non-empty {key!r}.")

    def transition_to(self, next_status: TaskStatus | str) -> None:
        """Move this task to a permitted next state."""

        try:
            normalized_status = (
                next_status
                if isinstance(next_status, TaskStatus)
                else TaskStatus(next_status)
            )
        except (TypeError, ValueError) as error:
            raise InvalidTaskTransitionError(f"Unknown task status: {next_status!r}") from error

        if normalized_status not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTaskTransitionError(
                f"Cannot transition a task from {self.status.value!r} "
                f"to {normalized_status.value!r}."
            )
        self.status = normalized_status

    def fail(self, reason: str) -> None:
        """End a running or parsing task as failed and retain the diagnostic reason."""

        if not isinstance(reason, str) or not reason.strip():
            raise TaskValidationError("A failed task requires a non-empty failure reason.")
        self.transition_to(TaskStatus.FAILED)
        self.failure_reason = reason
        self.retry_after_seconds = None

    def pause_for_rate_limit(self, *, retry_after_seconds: int) -> None:
        """Pause a task until the source's rate-limit window has elapsed."""

        if isinstance(retry_after_seconds, bool) or not isinstance(retry_after_seconds, int):
            raise TaskValidationError("retry_after_seconds must be a positive integer.")
        if retry_after_seconds <= 0:
            raise TaskValidationError("retry_after_seconds must be a positive integer.")
        self.transition_to(TaskStatus.PAUSED_RATE_LIMIT)
        self.retry_after_seconds = retry_after_seconds
        self.failure_reason = None
