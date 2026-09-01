from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from grading_graph.schemas import CandidateResult, OverallLabel, QuestionResult, QuestionVerdict, RiskLevel, StudentStatus


def _as_result(value: QuestionResult | dict[str, Any]) -> QuestionResult:
    return value if isinstance(value, QuestionResult) else QuestionResult.model_validate(value)


def aggregate_overall(question_results: Iterable[QuestionResult | dict[str, Any]]) -> tuple[OverallLabel, StudentStatus, int]:
    results = [_as_result(value) for value in question_results]
    if not results:
        return OverallLabel.UNKNOWN, StudentStatus.REVIEW_REQUIRED, 1
    verdicts = [result.verdict for result in results]
    if QuestionVerdict.MISMATCH in verdicts:
        overall = OverallLabel.MISMATCH
        status = StudentStatus.REFERENCE_MISMATCH
    elif QuestionVerdict.UNREADABLE in verdicts:
        overall = OverallLabel.UNREADABLE
        status = StudentStatus.UNREADABLE
    else:
        incorrect_count = verdicts.count(QuestionVerdict.INCORRECT)
        if incorrect_count == 0 and all(verdict == QuestionVerdict.CORRECT for verdict in verdicts):
            overall = OverallLabel.ALL_CORRECT
        elif incorrect_count >= 2:
            overall = OverallLabel.MANY_ERRORS
        else:
            overall = OverallLabel.PARTIAL
        status = StudentStatus.CANDIDATE_READY

    unresolved = sum(1 for result in results if result.needs_verification or result.risk_level != RiskLevel.LOW)
    if unresolved and status not in {StudentStatus.REFERENCE_MISMATCH, StudentStatus.UNREADABLE}:
        status = StudentStatus.REVIEW_REQUIRED
    return overall, status, unresolved


def build_candidate(
    *,
    graph_version: str,
    run_id: str,
    assignment_id: str,
    student_id: str,
    question_results: dict[str, QuestionResult | dict[str, Any]],
    errors: list[dict[str, Any]] | None = None,
    budget_usage: dict[str, Any] | None = None,
) -> CandidateResult:
    normalized = {key: _as_result(value) for key, value in sorted(question_results.items())}
    overall, status, unresolved = aggregate_overall(normalized.values())
    errors = errors or []
    if errors and status not in {StudentStatus.REFERENCE_MISMATCH}:
        status = StudentStatus.REVIEW_REQUIRED
    unresolved += len(errors)
    return CandidateResult(
        graph_version=graph_version,
        run_id=run_id,
        assignment_id=assignment_id,
        student_id=student_id,
        status=status,
        overall=overall,
        question_results=normalized,
        unresolved_risk_count=unresolved,
        candidate_text="",
        errors=errors,
        budget_usage=budget_usage or {},
    )
