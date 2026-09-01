from __future__ import annotations

import json
from types import SimpleNamespace

from evaluation.metrics import evaluate_candidates
from evaluation.model_judge import MultimodalModelJudge, candidate_snapshot_hash
from evaluation.validate_model_judgments import validate_model_judgments
from grading_graph.provider import OpenAIResponsesProvider


class SequenceProvider:
    model = "independent-sota-judge"

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        self.images = []

    def complete_json(self, prompt, schema, image_ref=None):
        self.prompts.append(prompt)
        self.images.append(image_ref)
        return self.responses.pop(0)


def _responses(*, confidence=0.94, decisive=True):
    return [
        {
            "verdict": "incorrect",
            "confidence": 0.91,
            "readable": True,
            "evidence_pages": [1],
            "negative_sign_risk": True,
            "summary": "负号导致结果错误",
        },
        {
            "candidate_supported": False,
            "independent_judge_supported": True,
            "proposed_verdict": "incorrect",
            "decisive": True,
            "evidence_pages": [1],
            "reason_codes": ["missed_negative_sign"],
            "summary": "候选漏看负号",
        },
        {
            "verdict": "incorrect",
            "confidence": confidence,
            "decisive": decisive,
            "evidence_sufficient": True,
            "candidate_supported": False,
            "evidence_pages": [1],
            "reason_codes": ["missed_negative_sign"],
            "summary": "证据明确",
        },
    ]


def _judge(provider):
    return MultimodalModelJudge(provider).evaluate_question(
        assignment_id="第一周",
        student_hash="a" * 64,
        question_id="1.1",
        candidate_result={
            "verdict": "correct",
            "confidence": 0.9,
            "transcription": [{"page": 1, "text": "x=1"}],
            "evidence_refs": [],
            "rubric_decisions": [],
        },
        reference={"question_type": "calculation", "reference_answer": "x=-1", "critical_symbols": ["-"]},
        image_refs=[],
    )


def test_model_judge_blind_pass_hides_candidate_and_adjudicates() -> None:
    provider = SequenceProvider(_responses())
    result = _judge(provider)
    assert result["annotation_status"] == "model_confirmed"
    assert result["expected_verdict"] == "incorrect"
    assert result["candidate_supported"] is False
    assert "candidate=" not in provider.prompts[0]
    assert "candidate=" in provider.prompts[1]
    assert len(provider.prompts) == 3


def test_model_judge_excludes_low_confidence_disputes_from_score() -> None:
    result = _judge(SequenceProvider(_responses(confidence=0.6, decisive=False)))
    assert result["annotation_status"] == "model_disputed"
    assert result["expected_verdict"] is None
    assert result["scoreable"] is False


def test_metrics_can_use_model_judgments_without_teacher_gold() -> None:
    judgment = _judge(SequenceProvider(_responses()))
    report = evaluate_candidates(
        [],
        [{"student_hash": "a" * 64, "question_results": {"1.1": {"verdict": "correct"}}}],
        model_judgments=[judgment],
        reference_source="model",
    )
    assert report["reference_source"] == "model"
    assert report["model_confirmed_question_records"] == 1
    assert report["question_verdict_accuracy"]["denominator"] == 1
    assert report["question_verdict_accuracy"]["numerator"] == 0
    assert report["model_judge_candidate_support_rate"]["value"] == 0


def test_openai_responses_provider_sends_images_with_store_disabled(workspace_tmp_path) -> None:
    image = workspace_tmp_path / "page.png"
    image.write_bytes(b"png-bytes")
    captured = {}

    class Responses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_text=json.dumps({"ok": True}),
                usage=SimpleNamespace(input_tokens=11, output_tokens=7),
            )

    provider = OpenAIResponsesProvider(SimpleNamespace(responses=Responses()), model="judge-model")
    value = provider.complete_json(
        "judge",
        {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
        image_ref=[str(image)],
    )
    assert value == {"ok": True}
    assert captured["store"] is False
    assert captured["input"][0]["content"][1]["type"] == "input_image"
    assert captured["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert provider.usage.calls == 1
    assert provider.usage.input_tokens == 11


def test_model_judge_gate_accepts_confirmed_and_disputed_rows() -> None:
    confirmed = {
        "annotation_source": "independent_multimodal_model_judge",
        "annotation_status": "model_confirmed",
        "assignment_id": "week",
        "student_hash": "a" * 64,
        "question_id": "1",
        "expected_verdict": "incorrect",
        "scoreable": True,
        "judge_confidence": 0.9,
        "candidate_supported": False,
        "evidence_refs": ["page:1"],
        "passes": {"independent": {}, "critic": {}, "adjudicator": {}},
    }
    disputed = {
        **confirmed,
        "question_id": "2",
        "annotation_status": "model_disputed",
        "expected_verdict": None,
        "scoreable": False,
        "judge_confidence": 0.5,
        "evidence_refs": [],
    }

    report = validate_model_judgments([confirmed, disputed])

    assert report["status"] == "passed"
    assert report["confirmed_count"] == 1
    assert report["disputed_count"] == 1


def test_candidate_snapshot_hash_changes_when_candidate_changes() -> None:
    first = candidate_snapshot_hash({"verdict": "correct", "confidence": 0.9})
    second = candidate_snapshot_hash({"verdict": "partial", "confidence": 0.9})
    assert len(first) == 64
    assert first != second
