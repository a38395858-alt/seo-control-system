from __future__ import annotations

import unittest

from seo_control.application.agent_workflows import (
    AGENT_TOOL_ALLOWLIST,
    AgentWorkflowInputError,
    run_content_workflow,
    run_content_workflow_skeleton,
)


class ContentAgentWorkflowTests(unittest.TestCase):
    def test_skeleton_validates_project_scope_and_exposes_only_allowlisted_tools(self) -> None:
        result = run_content_workflow_skeleton(
            project_id=3,
            content_asset_id=12,
            requested_action="generate_content",
        )

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["current_node"], "prepare_workflow")
        self.assertEqual(result["allowed_tools"], sorted(AGENT_TOOL_ALLOWLIST))
        self.assertEqual([event["node"] for event in result["events"]], [
            "validate_project_context",
            "prepare_workflow",
        ])

    def test_skeleton_rejects_missing_or_cross_scope_prone_project_id(self) -> None:
        with self.assertRaisesRegex(AgentWorkflowInputError, "project_id"):
            run_content_workflow_skeleton(project_id=0, requested_action="generate_content")

    def test_skeleton_requires_an_explicit_action(self) -> None:
        with self.assertRaisesRegex(AgentWorkflowInputError, "requested_action"):
            run_content_workflow_skeleton(project_id=3, requested_action=" ")

    def test_implemented_prefix_loads_context_and_explainable_memories_through_tools(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke(name: str, payload: dict[str, object]) -> dict[str, object]:
            calls.append((name, payload))
            if name == "load_project_context":
                return {"project": {"id": 3}, "content_asset": {"title_snapshot": "Outdoor step lights", "keyword": "step lighting"}}
            return {"memories": [{"memory_id": 8, "selection_reason": "Matched topic terms: step"}]}

        result = run_content_workflow(
            project_id=3,
            content_asset_id=12,
            requested_action="generate_content",
            tool_invoker=invoke,
        )

        self.assertEqual(["load_project_context", "retrieve_project_memories"], [name for name, _payload in calls])
        self.assertEqual("Outdoor step lights step lighting", calls[1][1]["query"])
        self.assertEqual([8], [item["memory_id"] for item in result["retrieved_memories"]])
        self.assertEqual(
            ["validate_project_context", "load_project_context", "retrieve_project_memories", "prepare_workflow"],
            [event["node"] for event in result["events"]],
        )


if __name__ == "__main__":
    unittest.main()
