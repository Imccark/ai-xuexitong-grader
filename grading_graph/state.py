from __future__ import annotations

from typing import Any

from grading_graph.graph import CURRENT_GRAPH_SCHEMA_VERSION, GradingGraphState, migrate_graph_state


def merge_question_results(
    left: dict[str, dict[str, Any]] | None, right: dict[str, dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


__all__ = ["CURRENT_GRAPH_SCHEMA_VERSION", "GradingGraphState", "merge_question_results", "migrate_graph_state"]
