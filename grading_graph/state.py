from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from grading_graph.schemas import GraphState


CURRENT_GRAPH_SCHEMA_VERSION = "1.0"


def merge_question_results(
    left: dict[str, dict[str, Any]] | None, right: dict[str, dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class GradingGraphState(TypedDict, total=False):
    """LangGraph channels for the canonical :class:`GraphState` protocol."""

    schema_version: str
    graph_version: str
    preprocess_version: str
    run_id: str
    assignment_id: str
    student_id: str
    answer_manifest: dict[str, Any]
    pages: list[dict[str, Any]]
    page_observations: list[dict[str, Any]]
    local_layout: dict[str, Any]
    layout_audit: list[dict[str, Any]]
    question_jobs: dict[str, Any]
    transcriptions: dict[str, list[dict[str, Any]]]
    answer_texts: dict[str, str]
    evidence_registry: dict[str, dict[str, Any]]
    question_ids: list[str]
    question_results: Annotated[dict[str, dict[str, Any]], merge_question_results]
    risk_question_ids: list[str]
    ambiguities: list[dict[str, Any]]
    errors: Annotated[list[dict[str, Any]], operator.add]
    warnings: list[dict[str, Any]]
    budget: dict[str, Any]
    budget_usage: dict[str, int]
    retries: dict[str, int]
    audit: dict[str, Any]
    final_projection: dict[str, Any]
    candidate: dict[str, Any]


class ImageGradingGraphState(GradingGraphState, total=False):
    """Serializable launch fields used by the end-to-end parent graph."""

    processed_student_dir: str
    answer_manifest_path: str
    artifact_root: str
    local_layout_config: dict[str, Any]


def migrate_graph_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Migrate and strictly validate durable student state.

    Unknown future versions and unknown durable fields fail closed.  Reduced
    0.9 compatibility projections remain accepted, but the returned mapping
    always contains the complete 1.0 defaults defined by ``GraphState``.
    """

    normalized = dict(state or {})
    version = str(normalized.get("schema_version", "0.9"))
    if version not in {"0.9", CURRENT_GRAPH_SCHEMA_VERSION}:
        raise ValueError(f"unsupported graph state schema version: {version}")
    normalized["schema_version"] = CURRENT_GRAPH_SCHEMA_VERSION
    return GraphState.model_validate(normalized).model_dump(mode="json")


def graph_state_payload(state: dict[str, Any]) -> dict[str, Any]:
    """Drop parent-graph launch fields and return validated durable state."""

    allowed = GraphState.model_fields.keys()
    return migrate_graph_state({key: value for key, value in state.items() if key in allowed})


__all__ = [
    "CURRENT_GRAPH_SCHEMA_VERSION",
    "GradingGraphState",
    "ImageGradingGraphState",
    "graph_state_payload",
    "merge_question_results",
    "migrate_graph_state",
]
