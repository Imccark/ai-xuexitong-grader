from __future__ import annotations

from pathlib import Path

from app.grading_graph.graph import _question_image_refs, build_grading_graph
from app.grading_graph.nodes.evidence_gate import build_evidence_packet
from app.grading_graph.nodes.grader import QuestionGrader
from app.grading_graph.nodes.question_locator import QuestionLocator
from app.grading_graph.nodes.rubric_compiler import compile_atomic_rubrics, deterministic_rubric_verdict
from app.grading_graph.nodes.verifier import TargetedVerifier
from app.grading_graph.schemas import (
    AnswerSliceRef,
    Budget,
    EvidenceRef,
    QuestionJob,
    QuestionResult,
    TranscriptionSpan,
)


def _answer(question_id: str, *, question_type: str = "unknown", critical_symbols=None, rubrics=None):
    return AnswerSliceRef(
        question_id=question_id,
        artifact_ref=f"reference_slices/{question_id}.tex",
        sha256="a" * 64,
        character_count=10,
        question_type=question_type,
        critical_symbols=list(critical_symbols or []),
        rubric_items=list(rubrics or []),
    )


def _ref(span_id: str, page: int = 1, artifact_ref: str = "page.png") -> EvidenceRef:
    return EvidenceRef(
        span_id=span_id,
        page=page,
        bbox=(0, 0, 100, 100),
        artifact_ref=artifact_ref,
    )


def _span(span_id: str, page: int = 1, text: str = "x=-1") -> TranscriptionSpan:
    return TranscriptionSpan(
        span_id=span_id,
        page=page,
        bbox=(5, 5, 95, 95),
        text=text,
        readability="clear",
        confidence=0.95,
    )


def test_evidence_gate_distinguishes_missing_route_image_only_and_incomplete() -> None:
    missing = QuestionJob(question_id="q1", route="unreadable", answer_slice=_answer("q1"))
    assert build_evidence_packet(missing, [])["status"] == "missing_route"

    image_only = QuestionJob(
        question_id="q1",
        route="risk",
        roi_refs=[_ref("p1")],
        answer_slice=_answer("q1"),
    )
    packet = build_evidence_packet(image_only, [])
    assert packet["status"] == "image_only"
    assert packet["requires_rescue"] is True

    multi_page = QuestionJob(
        question_id="q1",
        route="risk",
        roi_refs=[_ref("p1", 1), _ref("p2", 2)],
        answer_slice=_answer("q1"),
    )
    packet = build_evidence_packet(multi_page, [_span("s1", 1)])
    assert packet["status"] == "incomplete"
    assert packet["missing_pages"] == [2]


def test_atomic_rubric_compiler_splits_proof_and_parameter_branches() -> None:
    proof_job = QuestionJob(
        question_id="1.1.6",
        question_type="proof",
        answer_slice=_answer("1.1.6", question_type="proof"),
    )
    proof = compile_atomic_rubrics(
        proof_job,
        "证明：$r' \\geq r$；因为只增加一列，$r' \\leq r+1$；所以 $0 \\leq r'-r \\leq 1$。",
    )
    assert [item["id"] for item in proof] == [
        "proof_lower_bound",
        "proof_one_column_bound",
        "proof_conclusion",
    ]

    branch_job = QuestionJob(question_id="1.1.4", answer_slice=_answer("1.1.4"))
    branches = compile_atomic_rubrics(
        branch_job,
        r"\begin{enumerate}\item 一般参数无解\item k=1 时的通解\item k=-2 时的通解\end{enumerate}",
    )
    assert [item["id"] for item in branches] == ["branch_1", "branch_2", "branch_3"]


def test_atomic_rubric_compiler_splits_rref_substance_matrix_and_columns() -> None:
    job = QuestionJob(question_id="1.1.1", answer_slice=_answer("1.1.1"))
    atoms = compile_atomic_rubrics(
        job,
        r"行变换得到 \operatorname{rref}(A)=\begin{pmatrix}1&0\\0&1\end{pmatrix}，主列为1,2，自由列为3。",
    )
    assert [item["id"] for item in atoms] == [
        "final_rref",
        "pivot_free_columns",
    ]


def test_rank_proof_rejects_neighboring_parameter_transcription() -> None:
    answer = AnswerSliceRef(
        question_id="1.1.6",
        artifact_ref="reference_slices/proof.tex",
        sha256="b" * 64,
        character_count=100,
        question_type="proof",
        deterministic_checks=["rank"],
    )
    job = QuestionJob(
        question_id="1.1.6",
        roi_refs=[_ref("p1-q2")],
        answer_slice=answer,
    )
    packet = build_evidence_packet(
        job,
        [_span("p1-span-6", text="当a≠3时，x1=-(b+2)/(a-3)")],
    )
    assert packet["semantic_compatible"] is False
    assert packet["status"] == "incomplete"
    assert packet["requires_rescue"] is True


def test_rref_gate_discards_only_neighboring_beta_expression() -> None:
    answer = AnswerSliceRef(
        question_id="1.1.1 (2)",
        artifact_ref="reference_slices/rref.tex",
        sha256="b" * 64,
        character_count=100,
        problem="求矩阵的行最简形，并指出主列和自由列。",
    )
    job = QuestionJob(
        question_id="1.1.1 (2)",
        roi_refs=[_ref("p1", 1), _ref("p4", 4)],
        answer_slice=answer,
    )
    packet = build_evidence_packet(
        job,
        [
            _span("neighbor", 1, text="β=α1-α2+α3，是向量组的线性表示"),
            _span("target", 4, text="RREF=[I|c]，主列1,2,3，自由列4"),
        ],
    )
    assert packet["semantic_compatible"] is None
    assert packet["status"] == "ready"
    assert packet["incompatible_span_ids"] == ["neighbor"]
    assert packet["span_count"] == 1
    assert packet["observed_span_count"] == 2


def test_evidence_gate_detects_missing_explicit_subpart_coverage() -> None:
    answer = AnswerSliceRef(
        question_id="1.2.2",
        artifact_ref="reference_slices/compound.tex",
        sha256="b" * 64,
        character_count=100,
        problem=r"\textbf{(1)} 判断线性无关。\textbf{(2)} 求 beta 的线性表示。",
    )
    job = QuestionJob(question_id="1.2.2", roi_refs=[_ref("p2")], answer_slice=answer)
    packet = build_evidence_packet(job, [_span("s1", text="(2) 线性无关")])
    assert packet["expected_subparts"] == ["1", "2"]
    assert packet["observed_subparts"] == ["2"]
    assert packet["subpart_coverage_complete"] is False
    assert packet["status"] == "incomplete"
    assert packet["requires_rescue"] is True


def test_graph_does_not_send_neighboring_span_to_rref_grader() -> None:
    answer = AnswerSliceRef(
        question_id="1.1.1 (2)",
        artifact_ref="reference_slices/rref.tex",
        sha256="b" * 64,
        character_count=100,
        problem="求矩阵的行最简形，并指出主列和自由列。",
        rubric_items=[{"id": "r1", "requirement": "行最简形及主列、自由列正确"}],
    )
    job = QuestionJob(
        question_id="1.1.1 (2)",
        roi_refs=[_ref("p1", 1), _ref("p4", 4)],
        answer_slice=answer,
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            assert "β=α1-α2+α3" not in prompt
            assert "RREF=[I|c]" in prompt
            return {
                "verdict": "correct",
                "confidence": 0.95,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": ["target"],
                "rubric_decisions": [{"rubric_id": "r1", "status": "correct"}],
            }

    output = build_grading_graph(Provider()).invoke(
        {
            "graph_version": "test",
            "run_id": "neighbor-filter",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"1.1.1 (2)": job},
            "transcriptions": {
                "1.1.1 (2)": [
                    _span("neighbor", 1, text="β=α1-α2+α3，是向量组的线性表示"),
                    _span("target", 4, text="RREF=[I|c]，主列1,2,3，自由列4"),
                ]
            },
            "answer_texts": {"1.1.1 (2)": "行最简形及主列、自由列正确"},
            "budget": Budget(max_calls=3, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    result = output["candidate"]["question_results"]["1.1.1 (2)"]
    assert result["verdict"] == "correct"


def test_atomic_rubric_aggregator_only_decides_complete_coverage() -> None:
    ref = _ref("s1").model_dump(mode="json")
    decisions = [
        {"rubric_id": "a", "status": "correct"},
        {"rubric_id": "b", "status": "incorrect", "evidence_refs": [ref]},
    ]
    assert deterministic_rubric_verdict(decisions, ["a", "b"]) == "partial"
    assert deterministic_rubric_verdict(decisions[:1], ["a", "b"]) is None
    assert deterministic_rubric_verdict(
        [{"rubric_id": "a", "status": "correct"}, {"rubric_id": "b", "status": "correct"}],
        ["a", "b"],
    ) == "correct"


def test_graph_rescues_image_only_question_instead_of_short_circuiting_unreadable(workspace_tmp_path) -> None:
    image_path = workspace_tmp_path / "normalized.png"
    image_path.write_bytes(b"image")
    job = QuestionJob(
        question_id="q1",
        route="unreadable",
        roi_refs=[_ref("p1", artifact_ref=str(image_path))],
        answer_slice=_answer("q1", rubrics=[{"id": "r1", "requirement": "x=-1"}]),
    )

    class Provider:
        calls = 0

        def complete_json(self, prompt, schema, image_ref=None):
            self.calls += 1
            return {
                "verdict": "correct",
                "confidence": 0.95,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": [],
                "rubric_decisions": [{"rubric_id": "r1", "status": "correct"}],
            }

    provider = Provider()
    output = build_grading_graph(provider).invoke(
        {
            "graph_version": "test",
            "run_id": "image-only-rescue",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"q1": job},
            "transcriptions": {"q1": []},
            "answer_texts": {"q1": "x=-1"},
            "budget": Budget(max_calls=3, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    result = output["candidate"]["question_results"]["q1"]
    assert result["verdict"] == "correct"
    assert result["evidence_status"] == "image_only"
    assert result["resolution_status"] == "rescued"
    assert provider.calls == 1


def test_provider_failure_is_not_semantically_recorded_as_handwriting_unreadable(workspace_tmp_path) -> None:
    image_path = workspace_tmp_path / "normalized.png"
    image_path.write_bytes(b"image")
    job = QuestionJob(
        question_id="q1",
        route="fast",
        roi_refs=[_ref("p1", artifact_ref=str(image_path))],
        answer_slice=_answer("q1"),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            raise RuntimeError("upstream rejected request")

    output = build_grading_graph(Provider(), max_retries=0).invoke(
        {
            "graph_version": "test",
            "run_id": "provider-failure",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"q1": job},
            "transcriptions": {"q1": [_span("s1")]},
            "budget": Budget(max_calls=3, max_input_tokens=10000, max_output_tokens=1000).model_dump(),
        }
    )
    result = output["candidate"]["question_results"]["q1"]
    assert result["evidence_status"] == "provider_error"
    assert result["resolution_status"] == "provider_failed"
    assert result["attempt_history"][-1]["outcome"] == "provider_error"


def test_verifier_cannot_downgrade_atomic_rubric_without_named_contradiction() -> None:
    job = QuestionJob(
        question_id="q1",
        route="risk",
        roi_refs=[_ref("p1")],
        answer_slice=_answer("q1", rubrics=[{"id": "r1", "requirement": "x=-1"}]),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "decisive": True,
                "verdict": "incorrect",
                "reason": "怀疑有误",
                "evidence_supported": True,
                "corrected_evidence_refs": ["s1"],
            }

    original = QuestionResult(
        question_id="q1",
        verdict="correct",
        confidence=0.8,
        needs_verification=True,
        risk_level="high",
        rubric_decisions=[{"rubric_id": "r1", "status": "correct"}],
    )
    result = TargetedVerifier(Provider(), max_rounds=1).verify(
        original,
        job=job,
        transcription=[_span("s1")],
        answer_text="x=-1",
    )
    assert result.verdict.value == "correct"
    assert result.needs_verification is True


def test_verifier_patches_named_atomic_rubric_before_changing_aggregate() -> None:
    job = QuestionJob(
        question_id="q1",
        route="risk",
        roi_refs=[_ref("p1")],
        answer_slice=_answer(
            "q1",
            rubrics=[
                {"id": "matrix", "requirement": "矩阵数值正确"},
                {"id": "columns", "requirement": "主列判断正确"},
            ],
        ),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "decisive": True,
                "verdict": "partial",
                "reason": "矩阵中漏写负号，但主列判断正确",
                "evidence_supported": True,
                "corrected_evidence_refs": ["s1"],
                "contradicted_rubric_ids": ["matrix"],
            }

    original = QuestionResult(
        question_id="q1",
        verdict="correct",
        confidence=0.8,
        needs_verification=True,
        risk_level="high",
        rubric_decisions=[
            {"rubric_id": "matrix", "status": "correct"},
            {"rubric_id": "columns", "status": "correct"},
        ],
    )
    result = TargetedVerifier(Provider(), max_rounds=1).verify(
        original,
        job=job,
        transcription=[_span("s1")],
        answer_text="参考答案",
    )
    assert result.verdict.value == "partial"
    assert [item.status for item in result.rubric_decisions] == ["partial", "correct"]
    assert result.needs_verification is False


def test_verifier_cannot_downgrade_correct_answer_with_empty_reason() -> None:
    job = QuestionJob(
        question_id="q1",
        route="risk",
        roi_refs=[_ref("p1")],
        answer_slice=_answer("q1", rubrics=[{"id": "r1", "requirement": "答案正确"}]),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "decisive": True,
                "verdict": "partial",
                "reason": "   ",
                "evidence_supported": True,
                "corrected_evidence_refs": ["s1"],
                "contradicted_rubric_ids": ["r1"],
            }

    original = QuestionResult(
        question_id="q1",
        verdict="correct",
        confidence=0.8,
        needs_verification=True,
        risk_level="high",
        evidence_refs=[_ref("s1")],
        rubric_decisions=[{"rubric_id": "r1", "status": "correct"}],
    )
    result = TargetedVerifier(Provider(), max_rounds=1).verify(
        original,
        job=job,
        transcription=[_span("s1")],
        answer_text="参考答案",
    )
    assert result.verdict.value == "correct"
    assert result.needs_verification is True


def test_verifier_cannot_upgrade_sign_sensitive_partial_without_sign_check() -> None:
    job = QuestionJob(
        question_id="q1",
        route="risk",
        roi_refs=[_ref("p1")],
        answer_slice=_answer(
            "q1",
            critical_symbols=["-"],
            rubrics=[{"id": "r1", "requirement": "矩阵负号正确"}],
        ),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "decisive": True,
                "verdict": "correct",
                "reason": "整体看起来正确",
                "evidence_supported": True,
                "negative_sign_checked": False,
                "corrected_evidence_refs": ["s1"],
            }

    original = QuestionResult(
        question_id="q1",
        verdict="partial",
        confidence=0.8,
        needs_verification=True,
        risk_level="high",
        evidence_refs=[_ref("s1")],
        rubric_decisions=[{"rubric_id": "r1", "status": "partial", "evidence_refs": [_ref("s1")]}],
    )
    result = TargetedVerifier(Provider(), max_rounds=1).verify(
        original,
        job=job,
        transcription=[_span("s1")],
        answer_text="参考答案",
    )
    assert result.verdict.value == "partial"
    assert result.needs_verification is True


def test_correct_whole_verdict_repairs_noncontradictory_unknown_rubric_status() -> None:
    job = QuestionJob(
        question_id="q1",
        roi_refs=[_ref("s1")],
        answer_slice=_answer("q1", rubrics=[{"id": "r1", "requirement": "x=-1"}]),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "correct",
                "confidence": 0.9,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": ["s1"],
                "rubric_decisions": [
                    {
                        "rubric_id": "r1",
                        "status": "unknown",
                        "reason": "该评分点满足",
                        "evidence_refs": ["s1"],
                    }
                ],
            }

    result = QuestionGrader(Provider()).grade(job, [_span("s1")])
    assert result.verdict.value == "correct"
    assert [item.status for item in result.rubric_decisions] == ["correct"]


def test_explicit_correct_substance_in_reason_cannot_be_labeled_fully_incorrect() -> None:
    job = QuestionJob(
        question_id="q1",
        roi_refs=[_ref("s1")],
        answer_slice=_answer("q1", rubrics=[{"id": "branch_1", "requirement": "分类并求解"}]),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "incorrect",
                "confidence": 0.8,
                "needs_verification": True,
                "risk_level": "high",
                "evidence_refs": ["s1"],
                "rubric_decisions": [
                    {
                        "rubric_id": "branch_1",
                        "status": "incorrect",
                        "reason": "虽然学生得出了正确结论，但推导不完整。",
                        "evidence_refs": ["s1"],
                    }
                ],
            }

    result = QuestionGrader(Provider()).grade(job, [_span("s1")])
    assert [item.status for item in result.rubric_decisions] == ["partial"]


def test_required_branch_conclusion_acknowledged_in_reason_gets_partial_credit() -> None:
    job = QuestionJob(
        question_id="q1",
        roi_refs=[_ref("s1")],
        answer_slice=_answer(
            "q1",
            rubrics=[{"id": "branch_3", "requirement": "当 a=3 且 b≠-2 时方程组无解"}],
        ),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "incorrect",
                "confidence": 0.8,
                "needs_verification": True,
                "risk_level": "high",
                "evidence_refs": ["s1"],
                "rubric_decisions": [
                    {
                        "rubric_id": "branch_3",
                        "status": "incorrect",
                        "reason": "学生写出了 a=3 且 b≠-2 时无解，但秩的推导错误。",
                        "evidence_refs": ["s1"],
                    }
                ],
            }

    result = QuestionGrader(Provider()).grade(job, [_span("s1")])
    assert result.verdict.value == "incorrect"
    assert [item.status for item in result.rubric_decisions] == ["partial"]


def test_image_only_incorrect_can_bind_to_located_roi() -> None:
    job = QuestionJob(
        question_id="q1",
        roi_refs=[_ref("located-q1-1")],
        route="risk",
        answer_slice=_answer("q1", rubrics=[{"id": "r1", "requirement": "给出证明"}]),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            assert "allowed_image_evidence" in prompt
            return {
                "verdict": "incorrect",
                "confidence": 0.9,
                "needs_verification": True,
                "risk_level": "high",
                "evidence_refs": [],
                "rubric_decisions": [],
            }

    result = QuestionGrader(Provider(), strict_evidence_gate=False).grade(
        job,
        [],
        image_ref="located.png",
    )
    assert result.verdict.value == "incorrect"
    assert result.evidence_refs[0].span_id == "located-q1-1"


def test_non_equivalent_student_and_reference_formulas_cannot_receive_full_credit() -> None:
    job = QuestionJob(
        question_id="q1",
        roi_refs=[_ref("s1")],
        answer_slice=_answer("q1", rubrics=[{"id": "branch_1", "requirement": "唯一解公式正确"}]),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "correct",
                "confidence": 0.9,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": ["s1"],
                "rubric_decisions": [
                    {
                        "rubric_id": "branch_1",
                        "status": "correct",
                        "reason": "学生将 x1 写为 -(b+2)/(a-3)，而参考答案为 -1 + 4(b+2)/(a-3)，但声称代入成立。",
                        "evidence_refs": ["s1"],
                    }
                ],
            }

    result = QuestionGrader(Provider()).grade(job, [_span("s1")])
    assert [item.status for item in result.rubric_decisions] == ["partial"]


def test_false_formula_equality_chain_cannot_receive_full_credit() -> None:
    job = QuestionJob(
        question_id="q1",
        roi_refs=[_ref("s1")],
        answer_slice=_answer("q1", rubrics=[{"id": "branch_1", "requirement": "唯一解公式正确"}]),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "correct",
                "confidence": 0.9,
                "needs_verification": False,
                "risk_level": "low",
                "evidence_refs": ["s1"],
                "rubric_decisions": [{
                    "rubric_id": "branch_1",
                    "status": "correct",
                    "reason": r"经代数验证，例如 $x_1 = \frac{a-5b-13}{a-3} = -1 + \frac{4(b+2)}{a-3}$，结论正确。",
                    "evidence_refs": ["s1"],
                }],
            }

    result = QuestionGrader(Provider()).grade(job, [_span("s1")])
    assert result.verdict.value == "correct"
    assert [item.status for item in result.rubric_decisions] == ["partial"]


def test_explicit_sign_error_in_correct_reason_is_repaired_to_partial() -> None:
    job = QuestionJob(
        question_id="q1",
        roi_refs=[_ref("s1")],
        answer_slice=_answer("q1", rubrics=[{"id": "final_rref", "requirement": "RREF 全部符号正确"}]),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "correct",
                "confidence": 0.9,
                "needs_verification": True,
                "risk_level": "high",
                "evidence_refs": ["s1"],
                "rubric_decisions": [
                    {
                        "rubric_id": "final_rref",
                        "status": "correct",
                        "reason": "学生在第一行第四列出现算术错误，写成1，正确应为-1，导致与参考答案不一致，但仍判为correct。",
                        "evidence_refs": ["s1"],
                    }
                ],
            }

    result = QuestionGrader(Provider()).grade(job, [_span("s1")])
    assert [item.status for item in result.rubric_decisions] == ["partial"]


def test_visible_blank_target_is_incorrect_not_unreadable() -> None:
    job = QuestionJob(
        question_id="q1",
        roi_refs=[_ref("located-q1-1")],
        answer_slice=_answer("q1", rubrics=[{"id": "proof", "requirement": "给出证明"}]),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "verdict": "unreadable",
                "confidence": 0.0,
                "needs_verification": True,
                "risk_level": "high",
                "evidence_refs": [],
                "rubric_decisions": [
                    {
                        "rubric_id": "proof",
                        "status": "unreadable",
                        "reason": "图像中目标题区域无实质作答，随后是相邻题内容。",
                        "evidence_refs": [],
                    }
                ],
            }

    result = QuestionGrader(Provider(), strict_evidence_gate=False).grade(job, [], image_ref="page.png")
    assert result.verdict.value == "incorrect"
    assert [item.status for item in result.rubric_decisions] == ["incorrect"]
    assert result.evidence_refs[0].span_id == "located-q1-1"


def test_verifier_can_upgrade_all_nonincorrect_atomic_rubrics_to_correct() -> None:
    job = QuestionJob(
        question_id="q1",
        route="risk",
        roi_refs=[_ref("s1")],
        answer_slice=_answer(
            "q1",
            rubrics=[
                {"id": "setup", "requirement": "列式正确"},
                {"id": "answer", "requirement": "最终答案正确"},
            ],
        ),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "decisive": False,
                "verdict": "correct",
                "reason": "列式与最终答案已充分建立正确结论。",
                "evidence_supported": True,
            }

    original = QuestionResult(
        question_id="q1",
        verdict="partial",
        confidence=0.8,
        needs_verification=True,
        risk_level="high",
        evidence_refs=[_ref("s1")],
        rubric_decisions=[
            {"rubric_id": "setup", "status": "correct"},
            {"rubric_id": "answer", "status": "partial", "evidence_refs": [_ref("s1")]},
        ],
    )
    result = TargetedVerifier(Provider(), max_rounds=1).verify(
        original,
        job=job,
        transcription=[_span("s1")],
        answer_text="参考答案",
    )
    assert result.verdict.value == "correct"
    assert [item.status for item in result.rubric_decisions] == ["correct", "correct"]


def test_graph_passes_compiled_atomic_rubrics_into_verifier() -> None:
    job = QuestionJob(
        question_id="q1",
        route="risk",
        roi_refs=[_ref("s1")],
        answer_slice=_answer("q1", rubrics=[{"id": "r1", "requirement": "legacy whole question"}]),
    )
    answer_text = r"\textbf{(1)} 列式正确 \textbf{(2)} 最终答案正确"

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            if "targeted verifier" in prompt:
                assert '"rubric_id": "subpart_1"' in prompt
                assert '"rubric_id": "subpart_2"' in prompt
                return {
                    "decisive": True,
                    "verdict": "correct",
                    "reason": "两个小问均正确。",
                    "evidence_supported": True,
                    "corrected_evidence_refs": ["s1"],
                }
            return {
                "verdict": "partial",
                "confidence": 0.8,
                "needs_verification": True,
                "risk_level": "high",
                "evidence_refs": ["s1"],
                "rubric_decisions": [
                    {"rubric_id": "subpart_1", "status": "correct", "reason": "正确", "evidence_refs": ["s1"]},
                    {"rubric_id": "subpart_2", "status": "partial", "reason": "疑似省略步骤", "evidence_refs": ["s1"]},
                ],
            }

    output = build_grading_graph(Provider()).invoke(
        {
            "graph_version": "test",
            "run_id": "compiled-rubric-verifier",
            "assignment_id": "第一周",
            "student_id": "student",
            "question_jobs": {"q1": job},
            "transcriptions": {"q1": [_span("s1")]},
            "answer_texts": {"q1": answer_text},
            "budget": Budget(max_calls=4, max_input_tokens=20000, max_output_tokens=2000).model_dump(),
        }
    )
    result = output["candidate"]["question_results"]["q1"]
    assert result["verdict"] == "correct"
    assert [item["status"] for item in result["rubric_decisions"]] == ["correct", "correct"]


def test_verifier_cannot_erase_known_partial_credit_without_refuting_it() -> None:
    job = QuestionJob(
        question_id="q1",
        route="risk",
        roi_refs=[_ref("p1")],
        answer_slice=_answer(
            "q1",
            rubrics=[
                {"id": "correct_step", "requirement": "正确步骤"},
                {"id": "wrong_step", "requirement": "错误步骤"},
            ],
        ),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "decisive": True,
                "verdict": "incorrect",
                "reason": "整体有误",
                "evidence_supported": True,
                "corrected_evidence_refs": ["s1"],
                "contradicted_rubric_ids": ["wrong_step"],
            }

    original = QuestionResult(
        question_id="q1",
        verdict="partial",
        confidence=0.8,
        needs_verification=True,
        risk_level="high",
        rubric_decisions=[
            {"rubric_id": "correct_step", "status": "correct"},
            {"rubric_id": "wrong_step", "status": "incorrect", "evidence_refs": [_ref("s1")]},
        ],
        evidence_refs=[_ref("s1")],
    )
    result = TargetedVerifier(Provider(), max_rounds=1).verify(
        original,
        job=job,
        transcription=[_span("s1")],
        answer_text="参考答案",
    )
    assert result.verdict.value == "partial"
    assert result.needs_verification is True


def test_negative_sign_risk_can_supply_normalized_and_enhanced_views(workspace_tmp_path) -> None:
    normalized = workspace_tmp_path / "normalized.png"
    enhanced = workspace_tmp_path / "enhanced.png"
    normalized.write_bytes(b"normalized")
    enhanced.write_bytes(b"enhanced")
    job = QuestionJob(
        question_id="q1",
        roi_refs=[_ref("p1", artifact_ref=str(normalized))],
        answer_slice=_answer("q1", critical_symbols=["-"]),
    )
    assert _question_image_refs(job, include_enhanced=True) == [str(normalized), str(enhanced)]


def test_answer_blind_locator_crops_full_page_before_rescue_grading(workspace_tmp_path) -> None:
    from PIL import Image

    page = workspace_tmp_path / "normalized.png"
    Image.new("RGB", (2000, 3000), "white").save(page)
    job = QuestionJob(
        question_id="1.1.1 (2)",
        pages=[1],
        roi_refs=[_ref("fallback-page-1", artifact_ref=str(page))],
        route="risk",
        answer_slice=_answer("1.1.1 (2)"),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            assert "答案盲" in prompt
            assert "reference_answer" not in prompt
            return {
                "found": True,
                "locations": [{"page": 1, "bbox": [100, 200, 900, 500], "confidence": 0.95}],
                "reason": "题号与作答连续",
            }

    located = QuestionLocator(Provider()).locate_and_crop(job)
    assert located is not None
    assert len(located.roi_refs) == 1
    crop_path = located.roi_refs[0].artifact_ref
    assert "located_1.1.1_2_1.png" in crop_path
    with Image.open(crop_path) as crop:
        assert crop.size == (1600, 900)


def test_locator_accepts_qwen_singleton_location_without_confidence(workspace_tmp_path) -> None:
    from PIL import Image

    page = workspace_tmp_path / "normalized.png"
    Image.new("RGB", (1000, 1000), "white").save(page)
    job = QuestionJob(
        question_id="q1",
        pages=[1],
        roi_refs=[_ref("fallback-page-1", artifact_ref=str(page))],
        route="risk",
        answer_slice=_answer("q1"),
    )

    class Provider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {"found": True, "locations": {"page": 1, "bbox": [10, 20, 900, 800]}}

    located = QuestionLocator(Provider()).locate_and_crop(job)
    assert located is not None
    assert Path(located.roi_refs[0].artifact_ref).is_file()
