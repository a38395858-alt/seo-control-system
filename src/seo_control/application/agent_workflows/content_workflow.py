"""Project-scoped LangGraph skeleton for SEO content-agent workflows.

This module deliberately contains no credentials, database connections, or
network clients.  Later workflow modules may call existing application
services only through these named, server-side tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph


WorkflowStatus = Literal["queued", "planning", "running", "waiting_input", "completed", "failed"]

AGENT_TOOL_ALLOWLIST = frozenset({
    "load_project_context",
    "retrieve_project_memories",
    "research_competitors",
    "extract_style_cards",
    "plan_content_blueprint",
    "generate_article",
    "review_article_quality",
    "prepare_publish",
})


class AgentWorkflowInputError(ValueError):
    """Raised before a workflow can access a project-scoped backend tool."""


class ContentWorkflowState(TypedDict, total=False):
    project_id: int
    content_asset_id: int | None
    requested_action: str
    status: WorkflowStatus
    current_node: str
    allowed_tools: list[str]
    events: list[dict[str, str]]
    error_summary: str | None


def _event(state: Mapping[str, Any], *, node: str, message: str) -> list[dict[str, str]]:
    previous = state.get("events")
    events = [dict(item) for item in previous] if isinstance(previous, list) else []
    events.append({"node": node, "message": message})
    return events


def _validate_project_context(state: ContentWorkflowState) -> ContentWorkflowState:
    project_id = state.get("project_id")
    if not isinstance(project_id, int) or project_id <= 0:
        raise AgentWorkflowInputError("content-agent workflows require a positive project_id")
    requested_action = state.get("requested_action")
    if not isinstance(requested_action, str) or not requested_action.strip():
        raise AgentWorkflowInputError("content-agent workflows require a requested_action")
    return {
        "status": "planning",
        "current_node": "validate_project_context",
        "allowed_tools": sorted(AGENT_TOOL_ALLOWLIST),
        "events": _event(
            state,
            node="validate_project_context",
            message="Project scope was validated; only server-side allowlisted tools may be used.",
        ),
        "error_summary": None,
    }


def _prepare_workflow(state: ContentWorkflowState) -> ContentWorkflowState:
    return {
        "status": "queued",
        "current_node": "prepare_workflow",
        "events": _event(
            state,
            node="prepare_workflow",
            message="Workflow skeleton is ready for durable task execution.",
        ),
    }


def build_content_workflow() -> Any:
    """Create the non-side-effecting root graph used by later agent modules."""

    graph = StateGraph(ContentWorkflowState)
    graph.add_node("validate_project_context", _validate_project_context)
    graph.add_node("prepare_workflow", _prepare_workflow)
    graph.add_edge(START, "validate_project_context")
    graph.add_edge("validate_project_context", "prepare_workflow")
    graph.add_edge("prepare_workflow", END)
    return graph.compile()


def run_content_workflow_skeleton(
    *, project_id: int, requested_action: str, content_asset_id: int | None = None
) -> ContentWorkflowState:
    """Validate a request and return a safe LangGraph workflow seed.

    Persistence, model routing, and tool execution are deliberately deferred to
    later modules.  The result is serializable and contains no secrets.
    """

    initial_state: ContentWorkflowState = {
        "project_id": project_id,
        "content_asset_id": content_asset_id,
        "requested_action": requested_action,
        "status": "queued",
        "current_node": "created",
        "events": [],
    }
    return build_content_workflow().invoke(initial_state)
