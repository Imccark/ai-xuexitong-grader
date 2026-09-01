from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from grading_graph.graph import GraphExecutionSettings, _symbol_audit_image_refs, build_grading_graph, migrate_graph_state
from grading_graph.adapters.batch import candidate_has_provider_error, run_student_candidate
from grading_graph.adapters.batch import run_candidate_states
from grading_graph.adapters.rerun import run_targeted_question_rerun
from grading_graph.checkpoint import open_sqlite_checkpointer
from grading_graph.nodes.symbol_auditor import SymbolAuditor
from grading_graph.nodes.math_checks import check_matrix_calculations, check_numeric_equations
from grading_graph.schemas import (
    AnswerSliceRef,
    Budget,
    CandidateResult,
    EvidenceRef,
    FileRef,
    PageArtifact,
    QuestionJob,
    QuestionResult,
    RiskLevel,
    SymbolCandidate,
    TranscriptionSpan,
    GraphState,
)
from grading_graph.state import GradingGraphState
from grading_graph.nodes.grader import GradingProviderError, QuestionGrader
from grading_graph.nodes.verifier import TargetedVerifier
from grading_graph.budget import RateLimitedJsonProvider
from grading_graph.cache import CachedJsonProvider, JsonResponseCache
from grading_graph.store import atomic_write_json
from grade_evaluator import safe_exception_type


FAKE_SK = "sk-" + "123456789012"
FAKE_PROJECT_SK = "sk-" + "proj-" + "123456789012"


def _job(question_id: str, route: str = "fast") -> QuestionJob:
    return QuestionJob(
        question_id=question_id,
        pages=[1],
        roi_refs=[EvidenceRef(span_id=f"{question_id}-roi", page=1, bbox=(0, 0, 100, 100), artifact_ref="page_1.png")],
        answer_slice=AnswerSliceRef(
            question_id=question_id,
            artifact_ref=f"slices/{question_id}.tex",
            sha256="b" * 64,
            character_count=10,
        ),
        route=route,
    )


def _span(question_id: str) -> TranscriptionSpan:
    return TranscriptionSpan(
        span_id=f"{question_id}-span",
        page=1,
        bbox=(10, 10, 90, 90),
        text="x = -1",
        readability="clear",
        confidence=0.95,
    )


class GraphProvider:
    def __init__(self, responses: dict[str, dict[str, Any]], failures: set[str] | None = None) -> None:
        self.responses = responses
        self.failures = failures or set()
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def complete_json(self, prompt: str, schema: dict[str, Any], image_ref: str | None = None) -> dict[str, Any]:
        self.prompts.append(prompt)
        question_id = next((key for key in self.responses if key in prompt), "")
        if "targeted verifier" in prompt:
            self.calls.append(f"verify:{question_id}")
            return {"decisive": True, "verdict": "correct", "reason": "符号与后续计算一致"}
        self.calls.append(question_id)
        if question_id in self.failures:
            raise TimeoutError(f"synthetic timeout {FAKE_SK} for {question_id}")
        return self.responses[question_id]


def test_graph_runs_questions_in_parallel_and_aggregates_deterministically() -> None:
    provider = GraphProvider(
        {
            "1.1.1": {"verdict": "correct", "confidence": 0.95, "needs_verification": False},
            "1.1.2": {
                "verdict": "incorrect",
                "confidence": 0.7,
                "needs_verification": True,
                "risk_level": "high",
                "rubric_decisions": [
                    {
                        "rubric_id": "r1",
                        "status": "incorrect",
                        "evidence_refs": [
                            {"span_id": "1.1.2-span", "page": 1, "bbox": [10, 10, 90, 90], "artifact_ref": "page_1.png"}
                        ],
                        "reason": "符号",
                    }
                ],
            },
        }
    )
    app = build_grading_graph(provider)
    result = app.invoke(
        {
            "graph_version": "test",
            "run_id": "run-1",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"1.1.1": _job("1.1.1"), "1.1.2": _job("1.1.2", "risk")},
            "transcriptions": {"1.1.1": [_span("1.1.1")], "1.1.2": [_span("1.1.2")]},
            "budget": Budget(max_calls=5, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    assert set(result["question_results"]) == {"1.1.1", "1.1.2"}
    # Atomic rubric decisions are the source of truth.  A whole-question
    # verifier response cannot silently erase an evidence-backed incorrect
    # rubric without returning a validated rubric-level update.
    assert result["candidate"]["overall"] == "partial"
    assert result["candidate"]["status"] == "review_required"
    # Both the explicit risk question and the negative-sign-sensitive
    # "correct" proposal receive an adversarial verification call.
    assert result["candidate"]["budget_usage"]["calls"] == 4
    assert any(call.startswith("verify:") for call in provider.calls)


def test_targeted_verifier_sees_answer_transcription_and_can_correct_with_evidence(workspace_tmp_path) -> None:
    image_path = workspace_tmp_path / "roi.png"
    image_path.write_bytes(b"image")

    class Provider:
        def __init__(self) -> None:
            self.prompt = ""
            self.image_ref = None

        def complete_json(self, prompt, schema, image_ref=None):
            self.prompt = prompt
            self.image_ref = image_ref
            return {
                "decisive": True,
                "verdict": "incorrect",
                "reason": "原图有负号",
                "evidence_supported": True,
                "negative_sign_checked": True,
                "corrected_evidence_refs": [
                    {"span_id": "1.1.1-span", "page": 1, "bbox": [10, 10, 90, 90], "artifact_ref": str(image_path)}
                ],
            }

    provider = Provider()
    result = TargetedVerifier(provider).verify(
        QuestionResult(question_id="1.1.1", verdict="correct", confidence=0.7, needs_verification=True, risk_level="high"),
        job=_job("1.1.1", "risk"),
        transcription=[_span("1.1.1")],
        answer_text="标准答案 x=-1",
        image_ref=str(image_path),
    )
    assert result.verdict.value == "incorrect"
    assert result.needs_verification is False
    assert result.verifier_result["corrected_by_agent_loop"] is True
    assert result.verifier_result["negative_sign_checked"] is True
    assert "标准答案 x=-1" in provider.prompt
    assert "x = -1" in provider.prompt
    assert "rubric_decisions" in provider.prompt
    assert "只有原图和标准答案明确显示与某 rubric 矛盾" in provider.prompt
    assert provider.image_ref == str(image_path)


def test_targeted_verifier_backs_off_between_transient_rounds(monkeypatch) -> None:
    class FlakyProvider:
        def __init__(self) -> None:
            self.calls = 0

        def complete_json(self, prompt, schema, image_ref=None):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary timeout")
            return {
                "decisive": True,
                "verdict": "correct",
                "reason": "evidence is clear",
                "evidence_supported": True,
                "corrected_evidence_refs": ["1.1.1-span"],
            }

    sleeps: list[float] = []
    monkeypatch.setattr("grading_graph.nodes.verifier.time.sleep", sleeps.append)
    provider = FlakyProvider()
    result = TargetedVerifier(provider, backoff_base=0.25).verify(
        QuestionResult(question_id="1.1.1", verdict="unreadable", confidence=0.0, needs_verification=True, risk_level="high"),
        job=_job("1.1.1", "risk"),
        transcription=[_span("1.1.1")],
    )
    assert provider.calls == 2
    assert sleeps == [0.25]
    assert result.verdict.value == "correct"
    assert result.needs_verification is False


def test_verifier_preserves_tentative_deduction_with_clear_span() -> None:
    class InconclusiveProvider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "decisive": False,
                "verdict": "partial",
                "reason": "证据字段不完整，但转写显示存在步骤缺失",
                "evidence_supported": False,
            }

    result = TargetedVerifier(InconclusiveProvider()).verify(
        QuestionResult(
            question_id="1.1.1",
            verdict="unreadable",
            confidence=0.0,
            needs_verification=True,
            risk_level="high",
        ),
        job=_job("1.1.1", "risk"),
        transcription=[_span("1.1.1")],
        answer_text="标准答案",
    )
    assert result.verdict.value == "partial"
    assert result.needs_verification is True
    assert result.risk_level.value == "high"
    assert result.verifier_result["tentative"] is True
    assert result.evidence_refs


def test_graph_isolates_a_failed_question_and_keeps_other_results() -> None:
    provider = GraphProvider(
        {"1.1.1": {"verdict": "correct", "confidence": 0.95}},
        failures={"1.1.2"},
    )
    app = build_grading_graph(provider)
    result = app.invoke(
        {
            "graph_version": "test",
            "run_id": "run-2",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"1.1.1": _job("1.1.1"), "1.1.2": _job("1.1.2")},
            "transcriptions": {"1.1.1": [_span("1.1.1")], "1.1.2": [_span("1.1.2")]},
            "budget": Budget(max_calls=5, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    assert result["question_results"]["1.1.1"]["verdict"] == "correct"
    assert result["candidate"]["status"] == "review_required"
    assert result["errors"]
    assert result["errors"][0] == {
        "stage": "grader",
        "question_id": "1.1.2",
        "error_type": "GradingProviderError",
    }
    assert "synthetic timeout" not in str(result)
    assert FAKE_SK not in str(result)


def test_verifier_failure_does_not_persist_provider_exception_text() -> None:
    class VerifierFailureProvider(GraphProvider):
        def complete_json(self, prompt, schema, image_ref=None):
            if "targeted verifier" in prompt:
                raise RuntimeError(f"provider secret {FAKE_PROJECT_SK} leaked")
            return super().complete_json(prompt, schema, image_ref=image_ref)

    provider = VerifierFailureProvider({"1.1.1": {"verdict": "correct", "confidence": 0.95, "needs_verification": True, "risk_level": "high"}})
    result = build_grading_graph(provider).invoke(
        {
            "graph_version": "test",
            "run_id": "verifier-secret-run",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"1.1.1": _job("1.1.1")},
            "transcriptions": {"1.1.1": [_span("1.1.1")]},
            "budget": Budget(max_calls=4, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    assert result["candidate"]["question_results"]["1.1.1"]["verifier_result"] == {
        "decisive": False,
        "error_type": "GradingProviderError",
    }
    assert "provider secret" not in str(result)
    assert FAKE_PROJECT_SK not in str(result)


def test_grader_rejects_model_deduction_without_evidence() -> None:
    provider = GraphProvider(
        {
            "1.1.1": {
                "verdict": "incorrect",
                "confidence": 0.9,
                "rubric_decisions": [{"rubric_id": "r1", "status": "incorrect", "reason": "无证据"}],
            }
        }
    )
    with pytest.raises(GradingProviderError, match="evidence"):
        QuestionGrader(provider).grade(_job("1.1.1"), [])


def test_non_strict_graph_binds_clear_span_when_qwen_omits_evidence() -> None:
    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "partial",
                "confidence": 0.8,
                "needs_verification": True,
                "risk_level": "high",
                "evidence_refs": [],
                "rubric_decisions": [],
            }

    result = QuestionGrader(Provider(), strict_evidence_gate=False).grade(
        _job("1.1.1"), [_span("1.1.1")]
    )
    assert result.verdict.value == "partial"
    assert [ref.span_id for ref in result.evidence_refs] == ["1.1.1-span"]
    assert result.needs_verification is True


def test_grader_accepts_qwen_rubric_items_alias_and_derives_routing_confidence() -> None:
    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "partial",
                "needs_verification": True,
                "risk_level": "high",
                "rubric_items": [
                    {
                        "criterion": "关键步骤",
                        "status": "met",
                        "evidence_refs": ["1.1.1-span"],
                    },
                    {
                        "criterion": "最终结论",
                        "status": "not_met",
                        "evidence_refs": ["1.1.1-span"],
                        "comment": "结论有一处错误",
                    },
                ],
            }

    result = QuestionGrader(Provider()).grade(_job("1.1.1"), [_span("1.1.1")])
    assert result.verdict.value == "partial"
    assert result.confidence == 0.8
    assert [ref.span_id for ref in result.evidence_refs] == ["1.1.1-span"]
    assert [item.status for item in result.rubric_decisions] == ["correct", "incorrect"]


def test_graph_enforces_shared_call_budget() -> None:
    provider = GraphProvider(
        {
            "1.1.1": {"verdict": "correct", "confidence": 0.95},
            "1.1.2": {"verdict": "correct", "confidence": 0.95},
        }
    )
    result = build_grading_graph(provider).invoke(
        {
            "graph_version": "test",
            "run_id": "budget-run",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"1.1.1": _job("1.1.1"), "1.1.2": _job("1.1.2")},
            "transcriptions": {},
            "budget": Budget(max_calls=1, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    assert result["budget_usage"]["calls"] == 1
    assert len(provider.calls) == 1
    assert result["candidate"]["status"] == "review_required"


def test_graph_passes_only_the_question_answer_slice_to_the_grader() -> None:
    provider = GraphProvider({"1.1.1": {"verdict": "correct", "confidence": 0.95}})
    build_grading_graph(provider).invoke(
        {
            "graph_version": "test",
            "run_id": "answer-slice-run",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"1.1.1": _job("1.1.1")},
            "transcriptions": {},
            "answer_texts": {"1.1.1": "本题专属标准答案切片"},
            "budget": Budget(max_calls=2, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    assert any("本题专属标准答案切片" in prompt for prompt in provider.prompts)


def test_graph_reuses_question_response_cache(workspace_tmp_path) -> None:
    provider = GraphProvider({"1.1.1": {"verdict": "correct", "confidence": 0.95}})
    app = build_grading_graph(provider, cache_dir=workspace_tmp_path / "cache")
    base = {
        "graph_version": "test",
        "assignment_id": "第一周",
        "student_id": "student",
        "question_jobs": {"1.1.1": _job("1.1.1")},
        "transcriptions": {},
        "budget": Budget(max_calls=2, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
    }
    app.invoke({**base, "run_id": "cache-1"})
    app.invoke({**base, "run_id": "cache-2"})
    assert len(provider.calls) == 1


def test_response_cache_invalidates_for_model_preprocess_and_image_content(workspace_tmp_path) -> None:
    class Provider:
        model = "model-a"

        def __init__(self) -> None:
            self.calls = 0

        def complete_json(self, prompt, schema, image_ref=None):
            self.calls += 1
            return {"call": self.calls}

    image_path = workspace_tmp_path / "page.png"
    image_path.write_bytes(b"first")
    provider = Provider()
    cache = JsonResponseCache(workspace_tmp_path / "cache", preprocess_version="pre-v1")
    first = CachedJsonProvider(provider, cache)
    assert first.complete_json("prompt", {}, image_ref=str(image_path)) == {"call": 1}
    assert first.complete_json("prompt", {}, image_ref=str(image_path)) == {"call": 1}

    image_path.write_bytes(b"second")
    assert first.complete_json("prompt", {}, image_ref=str(image_path)) == {"call": 2}
    provider.model = "model-b"
    second = CachedJsonProvider(provider, cache)
    assert second.complete_json("prompt", {}, image_ref=str(image_path)) == {"call": 3}
    other_preprocess_cache = JsonResponseCache(workspace_tmp_path / "cache", preprocess_version="pre-v2")
    third = CachedJsonProvider(provider, other_preprocess_cache)
    assert third.complete_json("prompt", {}, image_ref=str(image_path)) == {"call": 4}


def test_grader_retries_a_transient_timeout_without_retrying_invalid_evidence() -> None:
    class FlakyProvider:
        calls = 0

        def complete_json(self, prompt, schema, image_ref=None):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary timeout")
            return {"verdict": "correct", "confidence": 0.9}

    provider = FlakyProvider()
    result = QuestionGrader(provider, max_retries=1, backoff_base=0).grade(_job("1.1.1"), [])
    assert result.verdict.value == "correct"
    assert provider.calls == 2


def test_grader_retries_429_and_5xx_but_not_401() -> None:
    class StatusError(RuntimeError):
        def __init__(self, status_code: int) -> None:
            super().__init__(f"provider status {status_code}")
            self.status_code = status_code

    class FlakyStatusProvider:
        def __init__(self, failures: list[int]) -> None:
            self.failures = list(failures)
            self.calls = 0

        def complete_json(self, prompt, schema, image_ref=None):
            self.calls += 1
            if self.failures:
                raise StatusError(self.failures.pop(0))
            return {"verdict": "correct", "confidence": 0.9}

    retryable = FlakyStatusProvider([429, 503])
    assert QuestionGrader(retryable, max_retries=2, backoff_base=0).grade(_job("1.1.1"), []).verdict.value == "correct"
    assert retryable.calls == 3

    for status_code in (401, 403):
        non_retryable = FlakyStatusProvider([status_code])
        with pytest.raises(GradingProviderError, match="provider failed"):
            QuestionGrader(non_retryable, max_retries=2, backoff_base=0).grade(_job("1.1.1"), [])
        assert non_retryable.calls == 1


def test_provider_error_messages_and_chains_never_expose_provider_secret() -> None:
    secret = FAKE_PROJECT_SK

    class SecretProvider:
        def complete_json(self, prompt, schema, image_ref=None):
            raise RuntimeError(f"upstream rejected request: {secret}")

    with pytest.raises(GradingProviderError) as raised:
        QuestionGrader(SecretProvider(), max_retries=0).grade(_job("1.1.1"), [])
    error = raised.value
    assert secret not in str(error)
    assert error.__cause__ is None


def test_legacy_provider_failure_diagnostic_does_not_echo_exception_text() -> None:
    secret = FAKE_PROJECT_SK
    diagnostic = safe_exception_type("接口调用失败：", RuntimeError(f"upstream rejected request: {secret}"))
    assert diagnostic == "接口调用失败：RuntimeError"
    assert secret not in diagnostic


def test_grader_rejects_a_non_json_provider_response_without_retry() -> None:
    class NonJsonProvider:
        calls = 0

        def complete_json(self, prompt, schema, image_ref=None):
            self.calls += 1
            return ["not", "an", "object"]

    provider = NonJsonProvider()
    with pytest.raises(GradingProviderError, match="object"):
        QuestionGrader(provider).grade(_job("1.1.1"), [])
    assert provider.calls == 1


def test_grader_retries_only_missing_atomic_rubric_ids() -> None:
    class Provider:
        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        def complete_json(self, prompt, schema, image_ref=None):
            self.calls += 1
            self.prompts.append(prompt)
            decisions = [
                {
                    "rubric_id": "subpart_1",
                    "status": "correct",
                    "evidence_refs": [],
                    "reason": "第一问正确",
                }
            ]
            if self.calls == 2:
                decisions.append(
                    {
                        "rubric_id": "subpart_2",
                        "status": "correct",
                        "evidence_refs": [],
                        "reason": "第二问正确",
                    }
                )
            return {
                "verdict": "correct",
                "confidence": 0.9,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": [],
                "rubric_decisions": decisions,
            }

    provider = Provider()
    base = _job("q1")
    job = base.model_copy(
        update={
            "answer_slice": base.answer_slice.model_copy(
                update={
                    "rubric_items": [
                        {"id": "subpart_1", "requirement": "第一问"},
                        {"id": "subpart_2", "requirement": "第二问"},
                    ]
                }
            )
        }
    )
    result = QuestionGrader(provider, max_retries=2).grade(job, [_span("q1")])
    assert provider.calls == 2
    assert [item.rubric_id for item in result.rubric_decisions] == ["subpart_1", "subpart_2"]
    assert '"subpart_2"' in provider.prompts[1]


def test_grader_does_not_deduct_only_for_omitted_mechanical_steps() -> None:
    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "partial",
                "confidence": 0.9,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": ["q1-span"],
                "rubric_decisions": [
                    {
                        "rubric_id": "result",
                        "status": "partial",
                        "evidence_refs": ["q1-span"],
                        "reason": "学生正确列出方程并给出正确的最终结果，但是未展示具体计算步骤。",
                    }
                ],
            }

    base = _job("q1")
    job = base.model_copy(
        update={
            "answer_slice": base.answer_slice.model_copy(
                update={"rubric_items": [{"id": "result", "requirement": "求出线性表示"}]}
            )
        }
    )
    result = QuestionGrader(Provider()).grade(job, [_span("q1")])
    assert result.rubric_decisions[0].status == "correct"


def test_grader_keeps_partial_when_rubric_requires_derivation_steps() -> None:
    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "partial",
                "confidence": 0.9,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": ["q1-span"],
                "rubric_decisions": [
                    {
                        "rubric_id": "proof",
                        "status": "partial",
                        "evidence_refs": ["q1-span"],
                        "reason": "最终结论正确，但是未展示证明过程。",
                    }
                ],
            }

    base = _job("q1")
    job = base.model_copy(
        update={
            "answer_slice": base.answer_slice.model_copy(
                update={"rubric_items": [{"id": "proof", "requirement": "写出证明过程"}]}
            )
        }
    )
    result = QuestionGrader(Provider()).grade(job, [_span("q1")])
    assert result.rubric_decisions[0].status == "partial"


def test_grader_accepts_context_complete_beta_combination_without_redundant_prefix() -> None:
    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "partial",
                "confidence": 0.9,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": ["q1-span"],
                "rubric_decisions": [
                    {
                        "rubric_id": "subpart_2",
                        "status": "partial",
                        "evidence_refs": ["q1-span"],
                        "reason": "学生正确解得系数(1, -1, 1)，但最终式省略了β=，等式左边缺失。",
                    }
                ],
            }

    base = _job("q1")
    job = base.model_copy(
        update={
            "answer_slice": base.answer_slice.model_copy(
                update={"rubric_items": [{"id": "subpart_2", "requirement": "求 beta 的线性表示"}]}
            )
        }
    )
    result = QuestionGrader(Provider()).grade(job, [_span("q1")])
    assert result.rubric_decisions[0].status == "correct"


@pytest.mark.parametrize(
    "reason",
    [
        (
            r"学生书写形式为 $x_1 = \frac{a-5b-13}{a-3}$，但经代数验证与"
            r"参考答案 $x_1 = -1 + 4 \cdot \frac{b+2}{a-3}$ 完全等价。"
        ),
        (
            r"学生给出 $x_1 = -\frac{b+2}{a-3}$ 即 "
            r"$-1 + 4\frac{b+2}{a-3}$，与参考答案等价。"
        ),
        "学生公式似乎有误？等等，让我重新核对；大概率是正确答案。",
    ],
)
def test_grader_never_marks_self_contradictory_or_uncertain_reason_correct(reason: str) -> None:
    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "correct",
                "confidence": 0.95,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": ["q1-span"],
                "rubric_decisions": [
                    {
                        "rubric_id": "formula",
                        "status": "correct",
                        "evidence_refs": ["q1-span"],
                        "reason": reason,
                    }
                ],
            }

    base = _job("q1")
    job = base.model_copy(
        update={
            "answer_slice": base.answer_slice.model_copy(
                update={"rubric_items": [{"id": "formula", "requirement": "唯一解公式正确"}]}
            )
        }
    )
    result = QuestionGrader(Provider()).grade(job, [_span("q1")])
    assert result.rubric_decisions[0].status == "partial"


def test_symbol_auditor_preserves_uncertainty_when_not_decisive() -> None:
    class AuditorProvider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "symbol_candidates": [{"symbol": "minus", "confidence": 0.6}],
                "decisive": False,
                "reason": "原图笔画不完整",
            }

    span = _span("symbol")
    span = span.model_copy(update={"readability": "uncertain", "symbol_candidates": []})
    audited = SymbolAuditor(AuditorProvider()).audit(span)
    assert audited.readability == "uncertain"
    assert audited.symbol_candidates[0].symbol == "minus"


def test_symbol_auditor_replaces_clear_ocr_with_decisive_original_image_reread() -> None:
    class AuditorProvider:
        calls = 0

        def complete_json(self, prompt, schema, image_ref=None):
            self.calls += 1
            return {
                "symbol_candidates": [{"symbol": "minus", "confidence": 0.99}],
                "decisive": True,
                "reason": "原图负号清楚",
                "corrected_text": "x1=-1+4u+6v; x2=1-5u-7v",
            }

    provider = AuditorProvider()
    span = _span("symbol").model_copy(
        update={
            "text": "x1=1+4u+6v; x2=1-5u+7v",
            "readability": "clear",
            "confidence": 0.95,
            "symbol_candidates": [SymbolCandidate(symbol="minus", confidence=0.95)],
        }
    )
    audited = SymbolAuditor(provider).audit(span, image_ref="page.png")
    assert provider.calls == 1
    assert audited.text == "x1=-1+4u+6v; x2=1-5u-7v"


def test_graph_routes_uncertain_transcription_through_symbol_auditor() -> None:
    class AuditGraphProvider:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete_json(self, prompt, schema, image_ref=None):
            self.calls.append(prompt)
            if "symbol auditor" in prompt:
                return {
                    "symbol_candidates": [{"symbol": "minus", "confidence": 0.96}],
                    "decisive": True,
                    "reason": "局部清晰",
                }
            return {"verdict": "correct", "confidence": 0.95, "needs_verification": False, "risk_level": "low", "evidence_refs": [], "rubric_decisions": []}

    provider = AuditGraphProvider()
    uncertain = _span("1.1.1").model_copy(update={"readability": "uncertain"})
    job = _job("1.1.1").model_copy(
        update={
            "answer_slice": _job("1.1.1").answer_slice.model_copy(update={"critical_symbols": ["-"]})
        }
    )
    result = build_grading_graph(provider).invoke(
        {
            "graph_version": "test",
            "run_id": "symbol-graph-run",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"1.1.1": job},
            "transcriptions": {"1.1.1": [uncertain]},
            "budget": Budget(max_calls=3, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    span = result["candidate"]["question_results"]["1.1.1"]["transcription"][0]
    assert span["symbol_candidates"][0]["symbol"] == "minus"
    assert sum("symbol auditor" in prompt for prompt in provider.calls) == 1


def test_graph_bounds_symbol_audit_to_one_span_per_question() -> None:
    class AuditGraphProvider:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete_json(self, prompt, schema, image_ref=None):
            self.prompts.append(prompt)
            if "symbol auditor" in prompt:
                return {
                    "symbol_candidates": [{"symbol": "minus", "confidence": 0.96}],
                    "decisive": True,
                    "reason": "原图复核完成",
                    "corrected_text": "x1=-1",
                }
            return {
                "verdict": "correct",
                "confidence": 0.95,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": [],
                "rubric_decisions": [],
            }

    provider = AuditGraphProvider()
    job = _job("1.1.1").model_copy(
        update={
            "answer_slice": _job("1.1.1").answer_slice.model_copy(update={"critical_symbols": ["-"]})
        }
    )
    spans = [
        TranscriptionSpan(
            span_id=f"s{index}",
            page=1,
            bbox=(10, 10 + index * 20, 90, 25 + index * 20),
            text=f"x{index}=-1",
            readability="uncertain",
            confidence=0.7,
            symbol_candidates=[SymbolCandidate(symbol="minus", confidence=0.7)],
        )
        for index in range(3)
    ]
    build_grading_graph(provider).invoke(
        {
            "graph_version": "test",
            "run_id": "bounded-symbol-audit",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"1.1.1": job},
            "transcriptions": {"1.1.1": spans},
            "budget": Budget(max_calls=3, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    assert sum("symbol auditor" in prompt for prompt in provider.prompts) == 1


def test_graph_recovers_symbol_audit_failure_by_discarding_untrusted_text() -> None:
    class Provider:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete_json(self, prompt, schema, image_ref=None):
            self.prompts.append(prompt)
            if "symbol auditor" in prompt:
                raise TimeoutError("synthetic auxiliary timeout")
            if "targeted verifier" in prompt:
                return {
                    "decisive": True,
                    "verdict": "correct",
                    "reason": "已直接核对原图",
                    "negative_sign_checked": True,
                    "evidence_supported": True,
                }
            assert '"text": ""' in prompt
            return {
                "verdict": "correct",
                "confidence": 0.95,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": [],
                "rubric_decisions": [],
            }

    job = _job("1.1.1").model_copy(
        update={
            "answer_slice": _job("1.1.1").answer_slice.model_copy(
                update={"critical_symbols": ["-"]}
            )
        }
    )
    uncertain = _span("1.1.1").model_copy(
        update={
            "text": "x = 1",
            "readability": "uncertain",
            "symbol_candidates": [SymbolCandidate(symbol="minus", confidence=0.5)],
        }
    )
    result = build_grading_graph(Provider()).invoke(
        {
            "graph_version": "test",
            "run_id": "symbol-fallback",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"1.1.1": job},
            "transcriptions": {"1.1.1": [uncertain]},
            "budget": Budget(max_calls=4, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    question = result["candidate"]["question_results"]["1.1.1"]
    assert question["transcription"][0]["text"] == ""
    assert any(
        item["outcome"] == "provider_unavailable_original_image_fallback"
        for item in question["attempt_history"]
    )
    assert not any(error.get("stage") == "symbol_auditor" for error in result.get("errors", []))


def test_graph_uses_located_crop_and_full_page_when_explicit_subparts_are_incomplete(workspace_tmp_path) -> None:
    from PIL import Image

    page_path = workspace_tmp_path / "normalized.png"
    Image.new("RGB", (1000, 1400), "white").save(page_path)

    class Provider:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.grader_image_ref = None

        def complete_json(self, prompt, schema, image_ref=None):
            self.prompts.append(prompt)
            if "答案盲" in prompt:
                return {
                    "found": True,
                    "locations": [{"page": 1, "bbox": [50, 100, 950, 900], "confidence": 0.95}],
                    "reason": "完整题号和两小问位于此页",
                }
            self.grader_image_ref = image_ref
            return {
                "verdict": "correct",
                "confidence": 0.95,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": [],
                "rubric_decisions": [],
            }

    base = _job("1.2.2")
    job = base.model_copy(
        update={
            "roi_refs": [
                EvidenceRef(
                    span_id="p1-q1",
                    page=1,
                    bbox=(0, 0, 1000, 1400),
                    artifact_ref=str(page_path),
                )
            ],
            "answer_slice": base.answer_slice.model_copy(
                update={
                    "problem": r"\textbf{(1)} 判断线性无关；\textbf{(2)} 求 beta 的线性表示",
                    "reference_answer": "(1) 线性无关；(2) beta=alpha1-alpha2+alpha3",
                }
            )
        }
    )
    only_first_part = _span("1.2.2").model_copy(update={"text": "(1) 线性无关"})
    provider = Provider()
    page_ref = FileRef(path=str(page_path), sha256=hashlib.sha256(page_path.read_bytes()).hexdigest())
    result = build_grading_graph(provider).invoke(
        {
            "graph_version": "test",
            "run_id": "full-page-subpart-rescue",
            "assignment_id": "第一周",
            "student_id": "student",
            "pages": [
                PageArtifact(
                    page=1,
                    original=page_ref,
                    normalized=page_ref,
                    page_type="assignment",
                ).model_dump(mode="json")
            ],
            "question_jobs": {"1.2.2": job},
            "transcriptions": {"1.2.2": [only_first_part]},
            "budget": Budget(max_calls=3, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    question = result["candidate"]["question_results"]["1.2.2"]
    assert question["transcription"] == []
    assert question["attempt_history"][0] == {
        "stage": "question_locator",
        "outcome": "located_with_full_page_context",
    }
    assert isinstance(provider.grader_image_ref, list)
    assert len(provider.grader_image_ref) == 2
    assert any("located_1.2.2_1.png" in path for path in provider.grader_image_ref)
    assert str(page_path) in provider.grader_image_ref


def test_symbol_auditor_uses_small_upscaled_normalized_and_enhanced_crops(workspace_tmp_path) -> None:
    from PIL import Image

    page = workspace_tmp_path / "normalized.png"
    Image.new("RGB", (800, 1200), "white").save(page)
    job = _job("q1").model_copy(
        update={
            "roi_refs": [EvidenceRef(span_id="p1", page=1, bbox=(0, 0, 800, 1200), artifact_ref=str(page))]
        }
    )
    span = _span("q1").model_copy(update={"bbox": (100, 200, 350, 300)})
    refs = _symbol_audit_image_refs(span, job)
    assert isinstance(refs, list) and len(refs) == 2
    assert refs[0].endswith("symbol_q1-span_normalized.png")
    assert refs[1].endswith("symbol_q1-span_enhanced.png")
    with Image.open(refs[0]) as crop:
        assert crop.width >= 1200


def test_deterministic_math_check_flags_an_internally_inconsistent_numeric_equation() -> None:
    checks = check_numeric_equations("1 + 1 = 3")
    assert checks == [{"expression": "1 + 1=3", "consistent": False}]


def test_deterministic_matrix_checks_cover_shape_trace_and_determinant() -> None:
    checks = check_matrix_calculations(
        "[[1, 2], [3, 4]] = [[1, 2], [3, 4]]; tr([[1,2],[3,4]]) = 5; det([[1,2],[3,4]]) = -2"
    )
    assert len(checks) == 3
    assert all(item["consistent"] for item in checks)

    mismatch = check_matrix_calculations("[[1, 2], [3, 4]] = [[1, 2], [3, 5]]")
    assert mismatch == [{"kind": "matrix_equation", "left_shape": [2, 2], "right_shape": [2, 2], "consistent": False}]

    non_square = check_matrix_calculations("det([[1, 2, 3], [4, 5, 6]]) = 0")
    assert non_square == [{"kind": "matrix_det", "shape": [2, 3], "consistent": False, "reason": "non_square_matrix"}]


def test_batch_adapter_writes_candidate_without_overwriting_formal_result(workspace_tmp_path) -> None:
    formal_dir = workspace_tmp_path / "第一周" / "results"
    formal_dir.mkdir(parents=True)
    formal_path = formal_dir / "student-1.txt"
    formal_path.write_text("legacy-result", encoding="utf-8")
    before = formal_path.read_bytes()
    provider = GraphProvider({"1.1.1": {"verdict": "correct", "confidence": 0.95}})
    candidate = run_student_candidate(
        provider=provider,
        graph_input={
            "graph_version": "test",
            "run_id": "adapter-run",
            "assignment_id": "第一周",
            "student_id": "student-1",
            "question_jobs": {"1.1.1": _job("1.1.1")},
            "transcriptions": {"1.1.1": [_span("1.1.1")]},
            "budget": Budget(max_calls=2, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        },
        artifact_root=workspace_tmp_path / "第一周",
    )
    assert candidate.overall.value == "all_correct"
    assert formal_path.read_bytes() == before
    artifact_dir = workspace_tmp_path / "第一周" / "agent_artifacts" / hashlib.sha256(b"student-1").hexdigest()
    assert artifact_dir.is_dir()
    assert {path.name for path in artifact_dir.glob("*.json")} >= {
        "input_manifest.json",
        "page_quality.json",
        "page_evidence.json",
        "question_reviews.json",
        "risk_report.json",
        "run_audit.json",
        "candidate_result.json",
    }


def test_batch_adapter_is_idempotent_for_the_same_run(workspace_tmp_path) -> None:
    provider = GraphProvider({"1.1.1": {"verdict": "correct", "confidence": 0.95}})
    graph_input = {
        "graph_version": "test",
        "run_id": "idempotent-run",
        "assignment_id": "第一周",
        "student_id": "student-1",
        "question_jobs": {"1.1.1": _job("1.1.1")},
        "transcriptions": {"1.1.1": [_span("1.1.1")]},
        "budget": Budget(max_calls=2, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
    }
    first = run_student_candidate(provider=provider, graph_input=graph_input, artifact_root=workspace_tmp_path / "第一周")
    second = run_student_candidate(provider=provider, graph_input=graph_input, artifact_root=workspace_tmp_path / "第一周")

    assert first.run_id == second.run_id
    assert provider.calls == ["1.1.1"]


def test_batch_adapter_audit_records_cache_and_provider_usage(workspace_tmp_path) -> None:
    class Usage:
        calls = 7
        input_tokens = 11
        output_tokens = 13

    provider = GraphProvider({"1.1.1": {"verdict": "correct", "confidence": 0.95}})
    provider.usage = Usage()
    cache_dir = workspace_tmp_path / "cache"
    graph_input = {
        "graph_version": "test",
        "run_id": "audit-run",
        "assignment_id": "第一周",
        "student_id": "student-1",
        "question_jobs": {"1.1.1": _job("1.1.1")},
        "transcriptions": {"1.1.1": [_span("1.1.1")]},
        "budget": Budget(max_calls=2, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
    }
    run_student_candidate(
        provider=provider,
        graph_input=graph_input,
        artifact_root=workspace_tmp_path / "第一周",
        cache_dir=cache_dir,
    )
    audit_path = workspace_tmp_path / "第一周" / "agent_artifacts" / hashlib.sha256(b"student-1").hexdigest() / "run_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["cache_hits"] == 0
    assert audit["cache_misses"] >= 1
    assert audit["provider_usage"] == {"calls": 7, "input_tokens": 11, "output_tokens": 13}


def test_rate_limited_provider_bounds_inner_question_concurrency() -> None:
    class SlowProvider:
        def __init__(self) -> None:
            self.active = 0
            self.maximum = 0
            self.lock = threading.Lock()

        def complete_json(self, prompt, schema, image_ref=None):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            time.sleep(0.03)
            with self.lock:
                self.active -= 1
            return {"ok": True}

    provider = SlowProvider()
    limited = RateLimitedJsonProvider(provider, max_concurrency=1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda index: limited.complete_json(str(index), {}), range(4)))
    assert provider.maximum == 1


def test_candidate_batch_summary_reports_hashed_failures_without_exception_text(workspace_tmp_path) -> None:
    provider = GraphProvider({"1.1.1": {"verdict": "correct", "confidence": 0.95}})
    states = [
        {
            "run_id": "batch-1",
            "assignment_id": "第一周",
            "student_id": "student-1",
            "question_jobs": {"1.1.1": _job("1.1.1")},
            "transcriptions": {"1.1.1": [_span("1.1.1")]},
            "budget": Budget(max_calls=2, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        },
        {
            "run_id": "batch-2",
            "assignment_id": "第一周",
            "student_id": "student-2",
            "question_jobs": {"1.1.2": _job("1.1.2")},
            "transcriptions": {"1.1.2": [_span("1.1.2")]},
            "budget": Budget(max_calls=2, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
            "corrupt_extra": b"not-json",
        },
    ]
    summary = run_candidate_states(
        provider=provider,
        states=states,
        artifact_root=workspace_tmp_path / "第一周",
        checkpoint_dir=workspace_tmp_path / "checkpoints",
    )
    assert summary.processed == 2
    assert summary.succeeded == 1
    assert summary.failed == 1
    assert summary.failures[0]["student_hash"] == hashlib.sha256(b"student-2").hexdigest()
    assert summary.failures[0]["error_type"] == "ValueError"
    assert "synthetic timeout" not in str(summary)


def test_candidate_batch_stops_after_three_consecutive_provider_errors(workspace_tmp_path) -> None:
    class FailingProvider:
        def complete_json(self, prompt, schema, image_ref=None):
            raise RuntimeError(f"upstream 503 with {FAKE_PROJECT_SK}")

    states = [
        {
            "run_id": f"stop-{index}",
            "assignment_id": "第一周",
            "student_id": f"student-{index}",
            "question_jobs": {"1.1.1": _job("1.1.1")},
            "transcriptions": {"1.1.1": [_span("1.1.1")]},
            "budget": Budget(max_calls=5, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
        for index in range(5)
    ]
    summary = run_candidate_states(
        provider=FailingProvider(),
        states=states,
        artifact_root=workspace_tmp_path / "第一周",
        checkpoint_dir=workspace_tmp_path / "checkpoints",
    )
    assert summary.processed == 3
    assert summary.succeeded == 3
    assert summary.failed == 0
    assert summary.stop_reason == "three_consecutive_provider_errors"


def test_recovered_old_labels_do_not_hide_complete_fresh_provider_outage() -> None:
    result = QuestionResult(
        question_id="1.1.1",
        verdict="correct",
        confidence=0.9,
    )
    candidate = CandidateResult(
        graph_version="test",
        run_id="recovered-outage",
        assignment_id="第一周",
        student_id="student",
        status="review_required",
        overall="all_correct",
        question_results={"1.1.1": result},
        errors=[
            {
                "stage": "recovery",
                "question_id": "1.1.1",
                "error_type": "RecoveredProviderError",
                "original_error_type": "GradingProviderError",
            }
        ],
    )
    assert candidate_has_provider_error(candidate) is True


def test_targeted_rerun_grades_only_one_question_and_merges_the_new_run(workspace_tmp_path) -> None:
    manifest_root = workspace_tmp_path / "manifest" / "reference_slices"
    manifest_root.mkdir(parents=True)
    (manifest_root / "1.1.1.tex").write_text("x=1", encoding="utf-8")
    manifest_path = manifest_root.parent / "manifest.json"
    atomic_write_json(
        manifest_path,
        {
            "assignment_id": "第一周",
            "answer_hash": "a" * 64,
            "compiler_version": "test",
            "questions": {
                "1.1.1": {
                    "question_id": "1.1.1",
                    "artifact_ref": "reference_slices/1.1.1.tex",
                    "sha256": "b" * 64,
                    "character_count": 3,
                }
            },
        },
    )
    (workspace_tmp_path / "processed_images" / "student-1").mkdir(parents=True)
    (workspace_tmp_path / "processed_images" / "student-1" / "page_1.png").write_bytes(b"placeholder")
    original = CandidateResult(
        graph_version="test",
        run_id="run-old",
        assignment_id="第一周",
        student_id="student-1",
        status="review_required",
        overall="partial",
        unresolved_risk_count=1,
        question_results={
            "1.1.1": QuestionResult(
                question_id="1.1.1",
                verdict="partial",
                confidence=0.6,
                needs_verification=True,
                risk_level="high",
                evidence_refs=[EvidenceRef(span_id="span-1", page=1, bbox=(1, 1, 10, 10), artifact_ref="page_1.png")],
                transcription=[_span("1.1.1")],
            ),
            "1.1.2": QuestionResult(
                question_id="1.1.2",
                verdict="correct",
                confidence=0.95,
            ),
        },
    )
    provider = GraphProvider({"1.1.1": {"verdict": "correct", "confidence": 0.95}})
    replacement = run_targeted_question_rerun(
        provider=provider,
        week_dir=workspace_tmp_path,
        candidate=original,
        question_id="1.1.1",
        answer_manifest_path=manifest_path,
        run_id="run-new",
        budget=Budget(max_calls=2, max_input_tokens=10000, max_output_tokens=1000),
    )
    assert replacement.run_id == "run-new"
    assert replacement.question_results["1.1.1"].verdict.value == "correct"
    assert replacement.question_results["1.1.2"].verdict.value == "correct"
    assert replacement.unresolved_risk_count == 0
    assert provider.calls == ["1.1.1", "verify:1.1.1"]
    assert all("1.1.2" not in call for call in provider.calls)


def test_graph_checkpoint_can_resume_after_an_interrupted_stream(workspace_tmp_path) -> None:
    provider = GraphProvider({"1.1.1": {"verdict": "correct", "confidence": 0.95}})
    input_state = {
        "schema_version": "0.9",
        "graph_version": "test",
        "run_id": "checkpoint-run",
        "assignment_id": "第一周",
        "student_id": "student",
        "answer_manifest": {"answer_hash": "a" * 64},
        "pages": [{"page": 1, "original": "page_1.png"}],
        "page_observations": [{"page": 1, "page_type": "assignment"}],
        "ambiguities": [{"span_id": "span-1"}],
        "audit": {"input_hash": "b" * 64},
        "question_jobs": {"1.1.1": _job("1.1.1")},
        "transcriptions": {"1.1.1": [_span("1.1.1")]},
        "budget": Budget(max_calls=2, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
    }
    checkpoint_path = workspace_tmp_path / "checkpoints.sqlite"
    with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
        app = build_grading_graph(provider, checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "checkpoint-thread"}}
        stream = app.stream(input_state, config=config)
        first_event = next(stream)
        assert "prepare" in first_event
        stream.close()
        reopened = app.invoke(None, config=config)

    assert reopened["candidate"]["overall"] == "all_correct"
    assert reopened["pages"] == [{"page": 1, "original": "page_1.png"}]
    assert reopened["page_observations"] == [{"page": 1, "page_type": "assignment"}]
    assert reopened["ambiguities"] == [{"span_id": "span-1"}]
    assert reopened["schema_version"] == "1.0"
    assert provider.calls == ["1.1.1"]


def test_graph_state_migration_is_idempotent_and_rejects_future_versions() -> None:
    migrated = migrate_graph_state({"schema_version": "0.9", "question_jobs": {}})
    assert migrated["schema_version"] == "1.0"
    assert migrate_graph_state(migrated) == migrated
    with pytest.raises(ValueError, match="unsupported graph state schema version"):
        migrate_graph_state({"schema_version": "2.0"})


def test_runtime_channels_and_canonical_graph_state_cannot_drift() -> None:
    assert set(GradingGraphState.__annotations__) == set(GraphState.model_fields)


def test_pipeline_retry_and_verification_config_is_consumed_by_graph(monkeypatch) -> None:
    observed: dict[str, int] = {}

    def fake_grade(self, job, transcription, **kwargs):
        observed["provider_retries"] = self.max_retries
        observed["missing_rubric_retries"] = self.missing_rubric_retries
        return QuestionResult(
            question_id=QuestionJob.model_validate(job).question_id,
            verdict="correct",
            confidence=0.6,
            needs_verification=True,
            risk_level="high",
        )

    def fake_verify(self, result, **kwargs):
        observed["verification_rounds"] = self.max_rounds
        return result.model_copy(update={"needs_verification": False, "risk_level": RiskLevel.LOW})

    monkeypatch.setattr("grading_graph.graph.QuestionGrader.grade", fake_grade)
    monkeypatch.setattr("grading_graph.graph.TargetedVerifier.verify", fake_verify)
    settings = GraphExecutionSettings.from_pipeline_config(
        {
            "retry": {"max_attempts": 1},
            "agent_loop": {
                "missing_rubric_retry_max": 0,
                "max_verification_rounds": 1,
            },
        }
    )
    build_grading_graph(GraphProvider({}), execution_settings=settings).invoke(
        {
            "graph_version": "test",
            "run_id": "configured-graph",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"1.1.1": _job("1.1.1")},
            "transcriptions": {"1.1.1": [_span("1.1.1")]},
            "budget": Budget(max_calls=3, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    assert observed == {
        "provider_retries": 0,
        "missing_rubric_retries": 0,
        "verification_rounds": 1,
    }
