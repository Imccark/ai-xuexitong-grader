from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from app.grading_graph.adapters.legacy_result import (
    candidate_to_legacy_projection,
    legacy_json_to_candidate,
    legacy_text_to_candidate,
)
from app.grading_graph.checkpoint import open_sqlite_checkpointer
from app.grading_graph.schemas import (
    CandidateResult,
    EvidenceRef,
    FinalResult,
    GraphState,
    OverallLabel,
    QuestionResult,
    RubricDecision,
    StudentStatus,
    TeacherDecision,
)
from app.grading_graph.store import ArtifactStore, canonical_hash


def test_structured_candidate_round_trip_and_evidence_gate() -> None:
    evidence = EvidenceRef(
        span_id="p1-r1-l1",
        page=1,
        bbox=(10, 20, 100, 80),
        artifact_ref="第10周/agent_artifacts/student/page_evidence.json",
    )
    question = QuestionResult(
        question_id="1.1.1",
        verdict="incorrect",
        rubric_decisions=[
            RubricDecision(
                rubric_id="r1",
                status="incorrect",
                evidence_refs=[evidence],
                reason="符号错误",
            )
        ],
        evidence_refs=[evidence],
        confidence=0.8,
        needs_verification=True,
    )
    candidate = CandidateResult(
        schema_version="1.0",
        graph_version="phase1-test",
        run_id="run-1",
        assignment_id="第一周",
        student_id="student-local",
        status=StudentStatus.REVIEW_REQUIRED,
        overall=OverallLabel.PARTIAL,
        question_results={question.question_id: question},
        unresolved_risk_count=1,
    )

    restored = CandidateResult.model_validate_json(candidate.model_dump_json())
    assert restored == candidate
    with pytest.raises(ValueError, match="evidence_refs"):
        RubricDecision(rubric_id="r1", status="incorrect", evidence_refs=[])


def test_final_result_cannot_be_finalized_with_unresolved_risk() -> None:
    candidate = CandidateResult(
        schema_version="1.0",
        graph_version="test",
        run_id="run-1",
        assignment_id="第一周",
        student_id="student-local",
        status=StudentStatus.REVIEW_REQUIRED,
        overall=OverallLabel.PARTIAL,
        unresolved_risk_count=1,
    )
    with pytest.raises(ValueError, match="unresolved"):
        FinalResult(candidate=candidate, finalized=True, submit_ready=True)

    decision = TeacherDecision(
        question_id="1.1.1",
        action="accept",
        revision=1,
        teacher_id="teacher-local",
    )
    final = FinalResult(
        candidate=candidate.model_copy(update={"unresolved_risk_count": 0, "status": StudentStatus.CANDIDATE_READY}),
        decisions=[decision],
        finalized=True,
        submit_ready=True,
        finalized_by="teacher-local",
        finalized_at="2026-08-26T00:00:00+00:00",
    )
    assert final.finalized is True


def test_artifact_store_atomic_writes_and_secret_rejection(workspace_tmp_path: Path) -> None:
    store = ArtifactStore(workspace_tmp_path)
    for index in range(1000):
        store.write_json("第一周", "a" * 64, f"test-{index}.json", {"index": index})
    assert json.loads(store.read_json("第一周", "a" * 64, "test-999.json").read_text(encoding="utf-8"))["index"] == 999
    assert not list(workspace_tmp_path.rglob("*.tmp"))
    assert not list(workspace_tmp_path.rglob(".tmp-*"))

    with pytest.raises(ValueError, match="secret"):
        store.write_json("第一周", "a" * 64, "secret.json", {"value": "sk-" + "proj-" + "123456789012345"})
    with pytest.raises(ValueError, match="API key"):
        store.write_json("第一周", "a" * 64, "field.json", {"DASHSCOPE_API_KEY": "configured"})

    with pytest.raises(ValueError, match="graph state"):
        GraphState(
            graph_version="test",
            run_id="run-1",
            assignment_id="第一周",
            student_id="student-local",
            final_projection={"DASHSCOPE_API_KEY": "configured"},
        )


def test_sqlite_checkpointer_can_be_reopened_and_read(workspace_tmp_path: Path) -> None:
    class State(TypedDict):
        count: int

    def increment(state: State) -> dict[str, int]:
        return {"count": state["count"] + 1}

    builder = StateGraph(State)
    builder.add_node("increment", increment)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    config = {"configurable": {"thread_id": "student-thread"}}
    db_path = workspace_tmp_path / "checkpoints.sqlite"

    with open_sqlite_checkpointer(db_path) as saver:
        app = builder.compile(checkpointer=saver)
        assert app.invoke({"count": 0}, config)["count"] == 1

    with open_sqlite_checkpointer(db_path) as saver:
        app = builder.compile(checkpointer=saver)
        state = app.get_state(config)
        assert state.values["count"] == 1

    corrupt_path = workspace_tmp_path / "corrupt.sqlite"
    corrupt_path.write_bytes(b"not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        with open_sqlite_checkpointer(corrupt_path):
            pass


def test_legacy_projection_preserves_compatible_fields_and_metadata() -> None:
    result_text = """========================================
姓名/学号：student-local
整体情况：部分错误
错误细节：
1. 1.1.1：负号错误
证明题审查：
1. 1.1.2：步骤完整
改进建议：
1. 复核符号
========================================
"""
    candidate = legacy_text_to_candidate(
        result_text,
        student_id="student-local",
        assignment_id="第一周",
        output_format="姓名/学号：{student_name}\n整体情况：[全对 / 部分错误 / 错误较多]\n错误细节：\n证明题审查：\n改进建议：",
    )
    projection = candidate_to_legacy_projection(candidate, student_name="student-local")
    assert projection["student_name_or_id"] == "student-local"
    assert projection["overall"] == "部分错误"
    assert "error_details_by_question" in projection
    assert projection["agent_metadata"]["schema_version"] == "1.0"
    assert canonical_hash(projection) == canonical_hash(json.loads(json.dumps(projection, ensure_ascii=False)))


def test_agent_candidate_projection_builds_deterministic_read_only_feedback() -> None:
    evidence = EvidenceRef(
        span_id="p1-q1",
        page=1,
        bbox=(10, 10, 100, 80),
        artifact_ref="normalized.png",
    )
    candidate = CandidateResult(
        graph_version="langgraph-v3-evidence-first",
        run_id="run-formal",
        assignment_id="第一周",
        student_id="student-1",
        status=StudentStatus.CANDIDATE_READY,
        overall=OverallLabel.PARTIAL,
        question_results={
            "1.1.1": QuestionResult(
                question_id="1.1.1",
                verdict="partial",
                confidence=0.9,
                evidence_refs=[evidence],
                rubric_decisions=[
                    RubricDecision(
                        rubric_id="final_answer",
                        status="incorrect",
                        evidence_refs=[evidence],
                        reason="最终结果漏写负号。",
                    )
                ],
            )
        },
    )

    projection = candidate_to_legacy_projection(candidate, student_name="student-1")

    assert projection["overall"] == "部分错误"
    assert projection["error_details_by_question"] == {"1.1.1": ["最终结果漏写负号。"]}
    assert projection["modules"]["错误细节"]["items"] == ["**1.1.1**：最终结果漏写负号。"]
    assert projection["agent_metadata"]["formal_result_source"] == "candidate"
    assert projection["agent_metadata"]["read_only"] is True


def test_all_600_historical_result_pairs_migrate_without_writes() -> None:
    from app.project_config import load_runtime_config

    result_paths = sorted(Path.cwd().glob("第*周/results/*.txt"))
    assert len(result_paths) == 600
    migrated = 0
    for result_path in result_paths:
        assignment_id = result_path.parent.parent.name
        config = load_runtime_config(week=assignment_id)
        payload = json.loads(result_path.with_suffix(".json").read_text(encoding="utf-8"))
        candidate = legacy_json_to_candidate(
            payload,
            result_text=result_path.read_text(encoding="utf-8"),
            student_id=result_path.stem,
            assignment_id=assignment_id,
            output_format=config.subject.output_format,
        )
        projection = candidate_to_legacy_projection(candidate, student_name=result_path.stem)
        assert projection["overall"]
        migrated += 1
    assert migrated == 600


def test_week_shorthand_prefers_matching_assignment_manifest() -> None:
    from app.project_config import load_runtime_config

    config = load_runtime_config(week="第一周")
    assert config.assignment_id == "第一周"
    assert config.week_name == "第一周"
    assert config.answer_key_path.name == "answer.tex"
