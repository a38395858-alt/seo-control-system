"""Domain-level TDD specification for keyword research tasks.

The tests deliberately describe the public API before the implementation exists.
They must stay independent of databases, HTTP clients, and framework code.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


from seo_control.domain.research_tasks import (  # noqa: E402
    InvalidTaskTransitionError,
    ResearchTask,
    TaskSource,
    TaskStatus,
    TaskValidationError,
)


class ResearchTaskTests(unittest.TestCase):
    """Rules for the keyword-mining task lifecycle and source payloads."""

    @staticmethod
    def suggest_parameters(**overrides: object) -> dict[str, object]:
        parameters: dict[str, object] = {
            "seed_keywords": ["seo tools"],
            "hl": "zh-CN",
            "gl": "CN",
        }
        parameters.update(overrides)
        return parameters

    @staticmethod
    def ads_parameters(**overrides: object) -> dict[str, object]:
        parameters: dict[str, object] = {
            "language": "zh",
            "geo_target_ids": [2156],
        }
        parameters.update(overrides)
        return parameters

    def create_task(self, source: TaskSource | str, parameters: dict[str, object]) -> ResearchTask:
        return ResearchTask.create(source=source, parameters=parameters)

    def test_creates_tasks_for_each_supported_source_in_draft(self) -> None:
        cases = (
            (TaskSource.GOOGLE_ADS, self.ads_parameters()),
            (TaskSource.GOOGLE_SUGGEST, self.suggest_parameters()),
            (TaskSource.FILE_IMPORT, {"filename": "keywords.csv"}),
        )

        for source, parameters in cases:
            with self.subTest(source=source):
                task = self.create_task(source, parameters)

                self.assertEqual(task.source, source)
                self.assertEqual(task.status, TaskStatus.DRAFT)

    def test_rejects_an_unsupported_source(self) -> None:
        with self.assertRaises(TaskValidationError):
            self.create_task("third_party_scraper", {})

    def test_allows_the_normal_task_lifecycle(self) -> None:
        task = self.create_task(TaskSource.GOOGLE_SUGGEST, self.suggest_parameters())

        for expected_status in (
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.PARSING,
            TaskStatus.AWAITING_IMPORT,
            TaskStatus.COMPLETED,
        ):
            task.transition_to(expected_status)
            self.assertEqual(task.status, expected_status)

    def test_rejects_an_invalid_state_transition_without_changing_state(self) -> None:
        task = self.create_task(TaskSource.GOOGLE_SUGGEST, self.suggest_parameters())

        with self.assertRaises(InvalidTaskTransitionError):
            task.transition_to(TaskStatus.RUNNING)

        self.assertEqual(task.status, TaskStatus.DRAFT)

    def test_running_task_can_be_marked_failed_with_its_failure_reason(self) -> None:
        task = self.create_task(TaskSource.GOOGLE_ADS, self.ads_parameters())
        task.transition_to(TaskStatus.QUEUED)
        task.transition_to(TaskStatus.RUNNING)

        task.fail("Google Ads API request was rejected")

        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.failure_reason, "Google Ads API request was rejected")

    def test_running_task_can_be_paused_when_the_source_rate_limits_it(self) -> None:
        task = self.create_task(TaskSource.GOOGLE_SUGGEST, self.suggest_parameters())
        task.transition_to(TaskStatus.QUEUED)
        task.transition_to(TaskStatus.RUNNING)

        task.pause_for_rate_limit(retry_after_seconds=300)

        self.assertEqual(task.status, TaskStatus.PAUSED_RATE_LIMIT)
        self.assertEqual(task.retry_after_seconds, 300)

    def test_google_suggest_requires_seed_keywords_and_a_locale(self) -> None:
        invalid_parameters = (
            self.suggest_parameters(seed_keywords=[]),
            self.suggest_parameters(hl=""),
            self.suggest_parameters(gl=""),
        )

        for parameters in invalid_parameters:
            with self.subTest(parameters=parameters):
                with self.assertRaises(TaskValidationError):
                    self.create_task(TaskSource.GOOGLE_SUGGEST, parameters)

    def test_google_ads_requires_a_language_and_at_least_one_geographic_target(self) -> None:
        invalid_parameters = (
            self.ads_parameters(language=""),
            self.ads_parameters(geo_target_ids=[]),
            self.ads_parameters(geo_target_ids=None),
        )

        for parameters in invalid_parameters:
            with self.subTest(parameters=parameters):
                with self.assertRaises(TaskValidationError):
                    self.create_task(TaskSource.GOOGLE_ADS, parameters)


if __name__ == "__main__":
    unittest.main()
