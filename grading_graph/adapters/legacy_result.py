from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from grading_graph.schemas import CandidateResult, OverallLabel, StudentStatus, TeacherDecision
from run_batch_grading import parse_result_text


OVERALL_TO_LABEL = {
    "全对": OverallLabel.ALL_CORRECT,
    "全正确": OverallLabel.ALL_CORRECT,
    "部分错误": OverallLabel.PARTIAL,
    "错误较多": OverallLabel.MANY_ERRORS,
}
LABEL_TO_LEGACY = {
    OverallLabel.ALL_CORRECT: "全对",
    OverallLabel.PARTIAL: "部分错误",
    OverallLabel.MANY_ERRORS: "错误较多",
    OverallLabel.UNREADABLE: "图片不可辨认",
    OverallLabel.MISMATCH: "题目版本不匹配",
    OverallLabel.UNKNOWN: "批阅失败",
}


def _question_feedback(candidate: CandidateResult) -> tuple[list[str], dict[str, list[str]]]:
    feedback: list[str] = []
    by_question: dict[str, list[str]] = {}
    for question_id, result in sorted(candidate.question_results.items()):
        if result.verdict.value == "correct":
            continue
        reasons = [
            decision.reason.strip()
            for decision in result.rubric_decisions
            if decision.status in {"partial", "incorrect", "unreadable"} and decision.reason.strip()
        ]
        if not reasons:
            fallback = {
                "partial": "答案部分正确，但存在需要订正的步骤或结论。",
                "incorrect": "答案或关键推导不正确。",
                "unreadable": "现有图片无法可靠辨认该题答案。",
                "mismatch": "作业题目与当前标准答案版本不匹配。",
            }.get(result.verdict.value, "该题未能形成可靠结论。")
            reasons = [fallback]
        # Keep the exported feedback concise and deterministic. The complete
        # evidence/rubric audit remains in candidate_result.json.
        concise = "；".join(dict.fromkeys(reasons))
        entry = f"**{question_id}**：{concise}"
        feedback.append(entry)
        by_question[question_id] = reasons
    return feedback, by_question


def legacy_text_to_candidate(
    result_text: str,
    *,
    student_id: str,
    assignment_id: str,
    output_format: str,
    graph_version: str = "legacy-adapter-v1",
) -> CandidateResult:
    parsed = parse_result_text(result_text, output_format)
    overall_text = str(parsed.get("overall") or "").strip()
    overall = OVERALL_TO_LABEL.get(overall_text, OverallLabel.UNKNOWN)
    error_count = sum(len(items) for items in parsed.get("error_details_by_question", {}).values())
    status = StudentStatus.CANDIDATE_READY if overall == OverallLabel.ALL_CORRECT and error_count == 0 else StudentStatus.REVIEW_REQUIRED
    return CandidateResult(
        graph_version=graph_version,
        run_id=f"legacy-{assignment_id}-{student_id}",
        assignment_id=assignment_id,
        student_id=student_id,
        status=status,
        overall=overall,
        unresolved_risk_count=error_count,
        legacy_projection=parsed,
    )


def legacy_json_to_candidate(
    result_json: dict[str, Any],
    *,
    result_text: str,
    student_id: str,
    assignment_id: str,
    output_format: str,
    graph_version: str = "legacy-adapter-v1",
) -> CandidateResult:
    """Convert one existing TXT/JSON result pair without writing to disk."""
    candidate = legacy_text_to_candidate(
        result_text,
        student_id=student_id,
        assignment_id=assignment_id,
        output_format=output_format,
        graph_version=graph_version,
    )
    return candidate.model_copy(update={"legacy_projection": dict(result_json)})


def candidate_to_legacy_projection(candidate: CandidateResult, *, student_name: str) -> dict[str, Any]:
    projection = dict(candidate.legacy_projection) if isinstance(candidate.legacy_projection, dict) else {}
    projection["student_name_or_id"] = student_name
    projection["overall"] = LABEL_TO_LEGACY.get(candidate.overall, "需人工复核")
    feedback, feedback_by_question = _question_feedback(candidate)
    if not projection.get("modules"):
        if feedback:
            advice = [f"请结合原题订正 {question_id}，重点核对关键符号、计算和最终结论。" for question_id in feedback_by_question]
            detail_items = feedback
        else:
            advice = ["本次作业未发现明显错误，请继续保持完整、规范的书写。"]
            detail_items = ["未发现明显错误。"]
        projection["modules"] = {
            "错误细节": {"raw_text": "\n".join(detail_items), "items": detail_items},
            "改进建议": {"raw_text": "\n".join(advice), "items": advice},
        }
    projection["error_details_by_question"] = feedback_by_question
    projection.setdefault("proof_review_by_question", {})
    projection["agent_metadata"] = {
        "schema_version": candidate.schema_version,
        "graph_version": candidate.graph_version,
        "run_id": candidate.run_id,
        "status": candidate.status.value,
        "unresolved_risk_count": candidate.unresolved_risk_count,
        "question_count": len(candidate.question_results),
        "formal_result_source": "candidate",
        "read_only": True,
    }
    return projection


def finalize_candidate(
    candidate: CandidateResult,
    decisions: list[TeacherDecision],
    *,
    finalized_by: str,
) -> dict[str, Any]:
    from grading_graph.schemas import FinalResult

    updated = candidate.model_copy(update={"status": StudentStatus.FINALIZED, "unresolved_risk_count": 0})
    final = FinalResult(
        candidate=updated,
        decisions=decisions,
        finalized=True,
        submit_ready=True,
        finalized_by=finalized_by,
        finalized_at=datetime.now(timezone.utc).isoformat(),
    )
    return final.model_dump(mode="json")
