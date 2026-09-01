from __future__ import annotations

import json
import hashlib
from types import SimpleNamespace

from grading_graph.schemas import CandidateResult
from grading_graph.shadow import run_shadow
from run_batch_grading import apply_candidate_budget_overrides


def test_shadow_report_is_candidate_only_and_preserves_legacy_hashes(workspace_tmp_path) -> None:
    formal_result = workspace_tmp_path / "legacy.json"
    formal_result.write_text('{"overall":"全对"}\n', encoding="utf-8")
    candidate = CandidateResult(
        graph_version="test",
        run_id="run-1",
        assignment_id="第一周",
        student_id="student-1",
        status="candidate_ready",
        overall="all_correct",
    )
    report_path = workspace_tmp_path / "shadow.json"
    report = run_shadow(
        candidates=[candidate],
        legacy_payloads={"student-1": {"overall": "全对"}},
        formal_result_paths=[formal_result],
        report_path=report_path,
    )
    assert report["candidate_only"] is True
    assert report["formal_result_source"] == "legacy"
    assert report["formal_results_unchanged"] is True
    assert report["overall_matches"] == 1
    assert json.loads(formal_result.read_text(encoding="utf-8"))["overall"] == "全对"
    difference = report["differences"][0]
    assert "student_id" not in difference
    assert difference["student_hash"] == hashlib.sha256(b"student-1").hexdigest()


def test_candidate_budget_overrides_are_explicit_and_do_not_mutate_input() -> None:
    state = {
        "run_id": "run-1",
        "assignment_id": "第一周",
        "student_id": "student-1",
        "question_jobs": {"1.1.1": {}},
        "budget": {"max_calls": 20, "max_input_tokens": 50000, "max_output_tokens": 10000, "max_image_pixels": 100},
    }
    args = SimpleNamespace(max_calls=3, max_input_tokens=4000, max_output_tokens=800)
    overridden = apply_candidate_budget_overrides([state], args)[0]
    assert overridden["budget"] == {
        "max_calls": 3,
        "max_input_tokens": 4000,
        "max_output_tokens": 800,
        "max_image_pixels": 100,
    }
    assert state["budget"]["max_calls"] == 20
