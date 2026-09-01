from __future__ import annotations

from tools.evaluation.core.metrics import evaluate_candidates, student_hash, wilson_interval


def test_wilson_interval_reports_numerator_denominator_and_bounds() -> None:
    result = wilson_interval(9, 10)
    assert result["numerator"] == 9
    assert result["denominator"] == 10
    assert 0 <= result["wilson_95"][0] <= result["value"] <= result["wilson_95"][1] <= 1


def test_metrics_use_hashed_student_identity_and_count_evidence() -> None:
    candidate = {
        "student_id": "student-1",
        "overall": "partial",
        "question_results": {
            "1.1.1": {
                "verdict": "incorrect",
                "evidence_refs": [{"page": 1, "bbox": [1, 2, 3, 4]}],
            }
        },
        "budget_usage": {"input_tokens": 6, "output_tokens": 4},
    }
    student = student_hash("student-1")
    result = evaluate_candidates(
        [{"student_hash": student, "question_id": "1.1.1", "expected_verdict": "incorrect", "expected_overall": "partial"}],
        [candidate],
        legacy_usage={student: {"input_tokens": 20, "output_tokens": 0}},
    )
    assert result["question_verdict_accuracy"]["numerator"] == 1
    assert result["overall_accuracy"]["numerator"] == 1
    assert result["no_evidence_deductions"]["numerator"] == 0
    assert result["question_coverage_recall"]["numerator"] == 1
    assert result["question_coverage_recall"]["denominator"] == 1
    assert result["error_accusation_false_positive_rate"]["value"] is None
    assert result["severe_misjudgment_rate"]["numerator"] == 0
    assert result["average_token_ratio"]["value"] == 0.5
    assert result["average_token_ratio"]["matched_students"] == 1
    assert result["p95_token_ratio"]["value"] == 0.5
    assert result["graph_failure_rate"]["value"] is None


def test_metrics_do_not_claim_accuracy_without_teacher_gold() -> None:
    result = evaluate_candidates([], [])
    assert result["question_verdict_accuracy"]["value"] is None
    assert result["overall_accuracy"]["value"] is None


def test_model_reference_does_not_relabel_model_rows_as_teacher_false_positives() -> None:
    student = student_hash("student-1")
    candidate = {
        "student_hash": student,
        "question_results": {
            "1.1.1": {
                "verdict": "incorrect",
                "rubric_decisions": [{"rubric_id": "r1", "status": "incorrect", "evidence_refs": [{"page": 1, "bbox": [1, 2, 3, 4]}]}],
            }
        },
    }
    model_row = {
        "annotation_source": "independent_multimodal_model_judge",
        "annotation_status": "model_confirmed",
        "scoreable": True,
        "student_hash": student,
        "question_id": "1.1.1",
        "expected_verdict": "correct",
    }
    result = evaluate_candidates([], [candidate], model_judgments=[model_row], reference_source="model")
    assert result["error_accusation_false_positive_rate"]["value"] is None
    assert result["error_accusation_false_positive_rate"]["status"] == "unmeasured"
