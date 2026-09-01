from __future__ import annotations

import pytest

from app.grading_graph.review import ReviewConflict, ReviewGateError, ReviewStore
from app.grading_graph.schemas import CandidateResult, EvidenceRef, OverallLabel, QuestionResult, StudentStatus, TeacherDecision


def _candidate() -> CandidateResult:
    return CandidateResult(
        graph_version="test",
        run_id="run-1",
        assignment_id="第一周",
        student_id="student-1",
        status="review_required",
        overall="partial",
        unresolved_risk_count=1,
        question_results={
            "1.1.1": QuestionResult(
                question_id="1.1.1",
                verdict="partial",
                confidence=0.7,
                needs_verification=True,
                risk_level="high",
                evidence_refs=[
                    EvidenceRef(span_id="span-1", page=1, bbox=(1, 1, 10, 10), artifact_ref="page_1.png")
                ],
            )
        },
    )


def test_review_store_keeps_candidate_separate_and_enforces_finalize_gate(workspace_tmp_path) -> None:
    store = ReviewStore(workspace_tmp_path / "第一周")
    candidate = _candidate()
    store.save_candidate(candidate)

    with pytest.raises(ReviewGateError, match="unresolved"):
        store.finalize("student-1", teacher_id="teacher", expected_revision=1)

    accepted = store.record_decision(
        "student-1",
        TeacherDecision(question_id="1.1.1", action="accept", revision=2, teacher_id="teacher"),
        expected_revision=1,
    )
    assert accepted["revision"] == 2
    assert accepted["candidate"]["unresolved_risk_count"] == 0
    assert store.load_candidate("student-1").status.value == "review_required"

    finalized = store.finalize("student-1", teacher_id="teacher", expected_revision=2)
    assert finalized["submitReady"] is True
    assert store.can_submit("student-1") is True
    assert store.load_candidate("student-1").status.value == "review_required"
    first_receipt = store.prepare_submission("student-1")
    second_receipt = store.prepare_submission("student-1")
    assert first_receipt["already_prepared"] is False
    assert second_receipt["already_prepared"] is True
    assert second_receipt["submission_id"] == first_receipt["submission_id"]


def test_review_store_optimistic_lock_and_reopen(workspace_tmp_path) -> None:
    store = ReviewStore(workspace_tmp_path / "第二周")
    candidate = _candidate().model_copy(
        update={
            "student_id": "student-2",
            "unresolved_risk_count": 0,
            "status": StudentStatus.CANDIDATE_READY,
            "overall": OverallLabel.ALL_CORRECT,
        }
    )
    store.save_candidate(candidate)
    store.finalize("student-2", teacher_id="teacher", expected_revision=1)

    with pytest.raises(ReviewConflict, match="finalized"):
        store.record_decision(
            "student-2",
            TeacherDecision(question_id="1.1.1", action="accept", revision=2, teacher_id="teacher"),
            expected_revision=1,
        )
    reopened = store.reopen("student-2", teacher_id="teacher", expected_revision=1)
    assert reopened["submitReady"] is False
    assert reopened["revision"] == 2
    assert store.can_submit("student-2") is False


def test_candidate_replacement_versions_old_result_and_respects_finalization_gate(workspace_tmp_path) -> None:
    store = ReviewStore(workspace_tmp_path / "第一周")
    first = _candidate().model_copy(update={"run_id": "run-old"})
    store.save_candidate(first)
    replacement = first.model_copy(update={"run_id": "run-new"})
    store.save_candidate(replacement)
    version_path = (
        workspace_tmp_path
        / "第一周"
        / "agent_artifacts"
        / store.student_hash("student-1")
        / "candidate_versions"
    )
    assert len(list(version_path.glob("*.json"))) == 1
    assert store.load_candidate("student-1").run_id == "run-new"

    accepted = store.record_decision(
        "student-1",
        TeacherDecision(question_id="1.1.1", action="accept", revision=1, teacher_id="teacher"),
        expected_revision=1,
    )
    store.finalize("student-1", teacher_id="teacher", expected_revision=accepted["revision"])
    with pytest.raises(ReviewGateError, match="finalized"):
        store.save_candidate(replacement.model_copy(update={"run_id": "run-after-final"}))
    with pytest.raises(ReviewGateError, match="finalized"):
        store.save_candidate(replacement)


def test_review_store_revalidates_edited_verdict_evidence_gate(workspace_tmp_path) -> None:
    store = ReviewStore(workspace_tmp_path / "第三周")
    candidate = _candidate().model_copy(
        update={
            "student_id": "student-3",
            "overall": OverallLabel.ALL_CORRECT,
            "unresolved_risk_count": 0,
            "status": StudentStatus.CANDIDATE_READY,
            "question_results": {
                "1.1.1": QuestionResult(
                    question_id="1.1.1",
                    verdict="correct",
                    confidence=0.95,
                    needs_verification=False,
                    risk_level="low",
                )
            },
        }
    )
    store.save_candidate(candidate)

    with pytest.raises(ReviewGateError, match="evidence gate"):
        store.record_decision(
            "student-3",
            TeacherDecision(
                question_id="1.1.1",
                action="edit",
                edited_verdict="incorrect",
                revision=2,
                teacher_id="teacher",
            ),
            expected_revision=1,
        )
