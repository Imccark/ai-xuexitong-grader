from __future__ import annotations

import json
import time
from typing import Any

from grading_graph.nodes.grader import GradingProvider, GradingProviderError
from grading_graph.nodes.rubric_compiler import deterministic_rubric_verdict
from grading_graph.schemas import EvidenceRef, QuestionJob, QuestionResult, QuestionVerdict, RiskLevel, TranscriptionSpan


VERIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decisive": {"type": "boolean"},
        "verdict": {"enum": [item.value for item in QuestionVerdict]},
        "reason": {"type": "string"},
        "evidence_supported": {"type": "boolean"},
        "negative_sign_checked": {"type": "boolean"},
        "corrected_evidence_refs": {"type": "array"},
        "contradicted_rubric_ids": {"type": "array"},
    },
    "required": ["decisive", "verdict", "reason"],
}


def _rubric_field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


class TargetedVerifier:
    def __init__(self, provider: GradingProvider, *, max_rounds: int = 2, backoff_base: float = 0.25) -> None:
        self.provider = provider
        self.max_rounds = max(1, min(max_rounds, 2))
        # A second adversarial round is deliberately bounded to two attempts,
        # but an immediate retry is particularly brittle with rate-limited
        # multimodal providers.  Backoff happens only for errors that look
        # transient, so malformed model JSON still fails fast and does not
        # consume extra wall-clock time.
        self.backoff_base = max(0.0, min(float(backoff_base), 5.0))

    def verify(
        self,
        result: QuestionResult,
        *,
        job: QuestionJob | dict[str, Any] | None = None,
        transcription: list[TranscriptionSpan | dict[str, Any]] | None = None,
        answer_text: str = "",
        image_ref: str | None = None,
    ) -> QuestionResult:
        question_job = QuestionJob.model_validate(job) if job is not None else None
        spans = [TranscriptionSpan.model_validate(value) for value in (transcription or result.transcription)]
        # Keep the verifier request deliberately small.  The previous prompt
        # duplicated the complete QuestionJob, transcription and candidate
        # result (including image paths and rubric payloads), which made the
        # multimodal request fragile and consumed most of the input budget.
        # The verifier only needs rubric identifiers, the answer text supplied
        # separately, compact spans and the candidate's proposed label.
        compact_job = {
            "question_id": question_job.question_id if question_job else result.question_id,
            "route": question_job.route if question_job else "risk",
            "rubric_items": [
                {
                    "rubric_id": str(item.get("rubric_id") or item.get("id") or f"r{index + 1}"),
                    "requirement": str(item.get("requirement") or item.get("description") or "")[:1800],
                }
                for index, item in enumerate(
                    question_job.answer_slice.rubric_items
                    if question_job and question_job.answer_slice
                    else []
                )
                if isinstance(item, dict)
            ],
            "critical_symbols": list(question_job.answer_slice.critical_symbols) if question_job and question_job.answer_slice else [],
        }
        compact_transcription = []
        for span in spans[:8]:
            item = span.model_dump(mode="json")
            item["text"] = str(item.get("text", ""))[:1200]
            compact_transcription.append(item)
        candidate_summary = {
            "verdict": result.verdict.value,
            "confidence": result.confidence,
            "needs_verification": result.needs_verification,
            "risk_level": result.risk_level.value,
            # The verifier receives the actual image and compact spans below;
            # serializing every absolute evidence path here wastes input
            # budget and can push large students over the per-student cap.
            "evidence_count": len(result.evidence_refs),
            "evidence_pages": sorted({ref.page for ref in result.evidence_refs}),
        }
        # Keep rubric coverage visible to the adversarial pass without copying
        # full evidence payloads into the prompt.  This lets the verifier
        # distinguish a genuine contradiction from a merely suspicious-looking
        # but already satisfied rubric, while retaining the image as the source
        # of truth for any downgrade.
        candidate_summary["rubric_decisions"] = [
            {
                "rubric_id": str(_rubric_field(item, "rubric_id", "") or _rubric_field(item, "id", "")),
                "status": str(_rubric_field(item, "status", "unknown") or "unknown"),
            }
            for item in (result.rubric_decisions or [])
        ]
        prompt = (
            "你是 targeted verifier，负责定向复核一个高风险题目。"
            "你必须扮演反方审计员：先尝试推翻 grader，再基于原图、标准答案和逐字转写裁决。"
            "只核对当前题，不扩大到其他题；重点逐个复查负号、小数点、等号、分数线、涂改和证明跳步。"
            "不能按数学合理性补写原图没有的笔画。若存在任何关键步骤正确但另有错误/遗漏，应优先判 partial，"
            "只有完全没有可评分正确实质时才判 incorrect；若最终结论被涂改后明确修正，以最终可读答案为准。"
            "对 rubric_items 逐条检查证据覆盖：要求多个分支/小问时，缺一项或证据在页底被截断应判 partial；"
            "参数名、自由变量名或线性表示的等价改写应判 correct。"
            "除非题目明示限定方法，否则行列式、秩、行变换等有效替代方法均可得满分；不得只因步骤或中间矩阵"
            "不同于参考答案而扣分。若设置与最终结论已充分建立答案，不得只因省略机械化计算步骤降为 partial。"
            "对上下文清楚的简写或参数化形式应先做代入/等价性复核。目标题号或定位区域清楚但无实质作答时判 incorrect，"
            "只有图像质量使是否作答都无法判断时才判 unreadable。"
            "涉及通解常数项、参数系数或正负号时，必须放大核对原图；转写只是候选文本，和原图冲突时以原图为准。"
            "若 candidate 的 rubric_decisions 全部为 correct，不得仅因格式不完整、怀疑漏写或未展示推导就降级；"
            "只有原图和标准答案明确显示与某 rubric 矛盾，并能用 corrected_evidence_refs 定位该矛盾时才可降级。"
            "若要把 correct 降为 partial/incorrect，或把 partial 降为 incorrect，必须同时输出 contradicted_rubric_ids，"
            "且只能填写当前 rubric_items 中的 id；partial 降为 incorrect 时必须逐一推翻所有原先 correct/partial 的评分点。"
            "当 candidate 已含 rubric_decisions 时，必须把反证精确绑定到 contradicted_rubric_ids；系统只会更新这些原子评分点，"
            "不会接受一个与逐点评分相矛盾的整题标签。"
            "若 verdict 改为 partial/incorrect，必须给出 corrected_evidence_refs；"
            "如果原图、答案或证据不足，decisive 必须为 false。输出严格 JSON。\n"
            f"question_id={result.question_id}\n"
            f"question_job={json.dumps(compact_job, ensure_ascii=False)}\n"
            f"answer_text={str(answer_text)[:4000]}\n"
            f"transcription={json.dumps(compact_transcription, ensure_ascii=False)}\n"
            f"candidate={json.dumps(candidate_summary, ensure_ascii=False)}"
        )
        last_payload: dict[str, Any] = {}
        last_error_type: str | None = None
        for round_index in range(self.max_rounds):
            try:
                payload = self.provider.complete_json(prompt, VERIFIER_SCHEMA, image_ref=image_ref)
            except Exception as exc:
                # A verifier failure must not erase a valid grader candidate or
                # abort the whole student run. Preserve the original result as
                # unresolved risk and expose the failure in verifier_result.
                last_error_type = type(exc).__name__
                if round_index + 1 < self.max_rounds and self._is_retryable(exc):
                    time.sleep(self.backoff_base * (2**round_index))
                continue
            if not isinstance(payload, dict):
                last_error_type = "InvalidVerifierResponse"
                continue
            last_payload = payload
            decisive = bool(payload.get("decisive", False))
            verdict = self._coerce_verdict(payload.get("verdict"), result.verdict)
            if verdict is None:
                # Qwen's JSON mode does not enforce the enum in our schema and
                # may return a short explanation in the verdict field. Treat
                # an unrecognizable label as inconclusive, but accept explicit
                # Chinese/English labels embedded in that explanation.
                continue
            # The grader's rubric array is an explicit lower-bound signal. If
            # it contains both satisfied and failed requirements, an overall
            # ``incorrect`` candidate cannot safely be upgraded to ``correct``
            # by a verifier that only saw a compact prompt: the contract for
            # ``partial`` is precisely "some scorable substance, plus an
            # error/omission". Keep this narrow invariant; do not generalize
            # it to all-correct arrays (that broader rule caused over-credit
            # on incomplete multi-branch answers in prior evaluations).
            if verdict is QuestionVerdict.CORRECT and result.verdict is QuestionVerdict.INCORRECT:
                statuses = {
                    str(_rubric_field(item, "status", "")).lower()
                    for item in (result.rubric_decisions or [])
                }
                if "correct" in statuses and ("partial" in statuses or "incorrect" in statuses):
                    verdict = QuestionVerdict.PARTIAL
                    rubric_refs = [
                        ref.model_dump(mode="json") if hasattr(ref, "model_dump") else ref
                        for item in (result.rubric_decisions or [])
                        for ref in (_rubric_field(item, "evidence_refs", []) or [])
                    ]
                    if rubric_refs:
                        payload["corrected_evidence_refs"] = rubric_refs
            if decisive:
                corrected_refs = self._normalize_evidence_refs(
                    payload.get("corrected_evidence_refs") or payload.get("evidence_refs"),
                    job=question_job,
                    spans=spans,
                )
                deduction_requires_refs = verdict in {QuestionVerdict.PARTIAL, QuestionVerdict.INCORRECT}
                evidence_supported = bool(payload.get("evidence_supported", bool(result.evidence_refs)))
                # An upgrade is still a grading change.  It must explain why
                # the prior deduction was wrong; for sign-sensitive questions
                # it must also attest that the original sign was rechecked.
                if result.verdict is QuestionVerdict.PARTIAL and verdict is QuestionVerdict.CORRECT:
                    if not str(payload.get("reason", "")).strip() or (
                        "-" in compact_job["critical_symbols"]
                        and not bool(payload.get("negative_sign_checked", False))
                    ):
                        decisive = False
                        payload["_unsupported_upgrade"] = True
                # A free-form second grader may not downgrade an evidence-
                # complete answer on suspicion alone.  It must identify the
                # exact atomic rubric it contradicts and point to the image
                # evidence for that contradiction.  This keeps the verifier
                # adversarial without allowing unsupported whole-question
                # relabeling.
                if result.verdict is QuestionVerdict.CORRECT and verdict in {
                    QuestionVerdict.PARTIAL,
                    QuestionVerdict.INCORRECT,
                }:
                    if not str(payload.get("reason", "")).strip():
                        decisive = False
                        payload["_unsupported_downgrade"] = True
                    valid_rubric_ids = {
                        str(item.get("rubric_id") or item.get("id") or "")
                        for item in compact_job["rubric_items"]
                    }
                    contradicted_ids = {
                        str(value)
                        for value in (payload.get("contradicted_rubric_ids") or [])
                    }
                    if valid_rubric_ids and (
                        not corrected_refs or not (contradicted_ids & valid_rubric_ids)
                    ):
                        decisive = False
                        payload["_unsupported_downgrade"] = True
                if result.verdict is QuestionVerdict.PARTIAL and verdict is QuestionVerdict.INCORRECT:
                    if not str(payload.get("reason", "")).strip():
                        decisive = False
                        payload["_unsupported_downgrade"] = True
                    positive_rubric_ids = {
                        str(_rubric_field(item, "rubric_id", "") or _rubric_field(item, "id", ""))
                        for item in (result.rubric_decisions or [])
                        if str(_rubric_field(item, "status", "")).lower() in {"correct", "partial"}
                    }
                    contradicted_ids = {
                        str(value)
                        for value in (payload.get("contradicted_rubric_ids") or [])
                    }
                    if positive_rubric_ids and (
                        not corrected_refs or not positive_rubric_ids.issubset(contradicted_ids)
                    ):
                        decisive = False
                        payload["_unsupported_downgrade"] = True
                if deduction_requires_refs and not (corrected_refs or result.evidence_refs) and not evidence_supported:
                    decisive = False
            # A verifier may return a tentative ``correct`` verdict without
            # explicitly setting decisive/evidence flags. When the transcription
            # itself contains a clear span, synthesize a bounded evidence ref
            # and allow that non-deductive correction to close the loop. We do
            # not apply this shortcut to partial/incorrect deductions.
            if (
                not decisive
                and not payload.get("_unsupported_downgrade")
                and not payload.get("_unsupported_upgrade")
                and verdict is QuestionVerdict.CORRECT
                and any(
                span.readability == "clear" for span in spans
                )
            ):
                corrected_refs = self._normalize_evidence_refs(
                    [span.span_id for span in spans if span.readability == "clear"],
                    job=question_job,
                    spans=spans,
                )
                decisive = bool(corrected_refs or result.evidence_refs)
            if decisive:
                updated_rubrics = list(result.rubric_decisions)
                if result.rubric_decisions and verdict is not result.verdict:
                    expected_ids = [
                        str(item.get("rubric_id") or item.get("id") or "")
                        for item in compact_job["rubric_items"]
                    ]
                    contradicted_ids = {
                        str(value)
                        for value in (payload.get("contradicted_rubric_ids") or [])
                    }
                    current_statuses = {
                        str(_rubric_field(item, "rubric_id", "")): str(_rubric_field(item, "status", "unknown"))
                        for item in result.rubric_decisions
                    }
                    patched: list[dict[str, Any]] | None = None
                    if verdict in {QuestionVerdict.PARTIAL, QuestionVerdict.INCORRECT}:
                        positive_ids = {
                            rubric_id
                            for rubric_id, status in current_statuses.items()
                            if status in {"correct", "partial"}
                        }
                        if (
                            corrected_refs
                            and contradicted_ids
                            and (verdict is not QuestionVerdict.INCORRECT or positive_ids.issubset(contradicted_ids))
                        ):
                            target_status = "incorrect" if verdict is QuestionVerdict.INCORRECT else "partial"
                            patched = []
                            for item in result.rubric_decisions:
                                dumped = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                                if str(dumped.get("rubric_id")) in contradicted_ids:
                                    dumped["status"] = target_status
                                    dumped["evidence_refs"] = [ref.model_dump(mode="json") for ref in corrected_refs]
                                    dumped["reason"] = str(payload.get("reason", ""))
                                patched.append(dumped)
                    elif verdict is QuestionVerdict.CORRECT and all(
                        status in {"correct", "partial", "unknown", "unreadable"}
                        for status in current_statuses.values()
                    ) and "incorrect" not in current_statuses.values():
                        patched = []
                        repair_refs = corrected_refs or result.evidence_refs
                        for item in result.rubric_decisions:
                            dumped = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                            dumped["status"] = "correct"
                            if repair_refs:
                                dumped["evidence_refs"] = [ref.model_dump(mode="json") for ref in repair_refs]
                            dumped["reason"] = str(payload.get("reason", ""))
                            patched.append(dumped)
                    if patched is not None:
                        aggregate = deterministic_rubric_verdict(patched, expected_ids)
                        if aggregate == verdict.value:
                            updated_rubrics = patched
                        else:
                            decisive = False
                            payload["_unsupported_rubric_patch"] = True
                    else:
                        decisive = False
                        payload["_unsupported_rubric_patch"] = True
            if decisive:
                return QuestionResult.model_validate(
                    {
                        **result.model_dump(mode="json"),
                        "verdict": verdict,
                        "rubric_decisions": updated_rubrics,
                        "evidence_refs": corrected_refs or result.evidence_refs,
                        "confidence": max(result.confidence, 0.8),
                        "needs_verification": False,
                        "risk_level": RiskLevel.LOW,
                        "verifier_result": {
                            "decisive": True,
                            "verdict": verdict.value,
                            "reason": str(payload.get("reason", "")),
                            "round": round_index + 1,
                            "evidence_supported": bool(payload.get("evidence_supported", True)),
                            "negative_sign_checked": bool(payload.get("negative_sign_checked", False)),
                            "candidate_verdict": result.verdict.value,
                            "corrected_by_agent_loop": verdict != result.verdict,
                        },
                    }
                )
        last_verdict = self._coerce_verdict(last_payload.get("verdict"), result.verdict) or result.verdict
        verifier_result = (
            {"decisive": False, "error_type": "GradingProviderError"}
            if last_error_type
            else {
                "decisive": False,
                "verdict": last_verdict.value,
                "reason": str(last_payload.get("reason", "")),
                "round": self.max_rounds,
            }
        )
        # Preserve an evidence-bearing tentative deduction instead of erasing
        # it as ``unreadable`` when the verifier itself is inconclusive.  This
        # is deliberately non-decisive: downstream aggregation keeps the high
        # risk flag and automatic submission remains blocked, while reports
        # can still measure the model's proposed partial/incorrect verdict.
        if (
            not last_error_type
            and not bool(last_payload.get("_unsupported_downgrade"))
            and not result.rubric_decisions
            and last_verdict in {QuestionVerdict.PARTIAL, QuestionVerdict.INCORRECT}
            and any(span.readability == "clear" for span in spans)
        ):
            tentative_refs = self._normalize_evidence_refs(
                [span.span_id for span in spans if span.readability == "clear"],
                job=question_job,
                spans=spans,
            )
            if tentative_refs or result.evidence_refs:
                verifier_result["tentative"] = True
                verifier_result["evidence_supported"] = True
                verifier_result["corrected_evidence_refs"] = [
                    ref.model_dump(mode="json") for ref in (tentative_refs or result.evidence_refs)
                ]
                return result.model_copy(
                    update={
                        "verdict": last_verdict,
                        "evidence_refs": tentative_refs or result.evidence_refs,
                        "needs_verification": True,
                        "risk_level": RiskLevel.HIGH,
                        "verifier_result": verifier_result,
                    }
                )
        return result.model_copy(
            update={
                "needs_verification": True,
                "risk_level": RiskLevel.HIGH,
                "verifier_result": verifier_result,
            }
        )

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Identify transient upstream failures without exposing exception text."""
        status_code = getattr(exc, "status_code", None)
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        if isinstance(status_code, int) and (status_code == 408 or status_code == 429 or 500 <= status_code <= 599):
            return True
        # OpenAI-compatible clients do not expose a stable exception class
        # across versions; retain a narrow status-token fallback for wrapped
        # HTTP errors while avoiding retries for auth/schema failures.
        error_text = str(exc).lower()
        return any(token in error_text for token in (" 408", " 429", " 500", " 502", " 503", " 504", "timeout", "timed out"))

    @staticmethod
    def _coerce_verdict(raw: Any, fallback: QuestionVerdict | None = None) -> QuestionVerdict | None:
        """Normalize enum labels embedded in free-form Qwen JSON output."""
        value = str(raw or "").strip().lower()
        if value in {item.value for item in QuestionVerdict}:
            return QuestionVerdict(value)
        if not value:
            return fallback
        # Check negative labels before ``correct`` because "incorrect"
        # contains the latter substring.
        if any(token in value for token in ("unreadable", "不可读", "无法辨认", "证据不足")):
            return QuestionVerdict.UNREADABLE
        if any(token in value for token in ("incorrect", "wrong", "错误", "不正确", "不对")):
            return QuestionVerdict.INCORRECT
        if any(token in value for token in ("partial", "partially", "部分", "不完整")):
            return QuestionVerdict.PARTIAL
        if any(token in value for token in ("correct", "right", "正确", "全对")):
            return QuestionVerdict.CORRECT
        return None

    @staticmethod
    def _normalize_evidence_refs(
        raw: Any,
        *,
        job: QuestionJob | None,
        spans: list[TranscriptionSpan],
    ) -> list[EvidenceRef]:
        """Accept compact span-id refs emitted by Qwen and rebuild full refs."""
        if not isinstance(raw, list):
            return []
        span_map = {span.span_id: span for span in spans}
        roi_refs = list(job.roi_refs) if job is not None else []
        roi_map = {ref.span_id: ref for ref in roi_refs}
        output: list[EvidenceRef] = []
        for item in raw:
            if isinstance(item, str):
                item = {"span_id": item.strip()}
            if not isinstance(item, dict):
                continue
            try:
                output.append(EvidenceRef.model_validate(item))
                continue
            except ValueError:
                pass
            span_id = str(item.get("span_id") or "")
            span = span_map.get(span_id)
            roi = roi_map.get(span_id)
            if span is not None:
                roi = roi or next((ref for ref in roi_refs if ref.page == span.page), None)
                if roi is not None:
                    output.append(
                        EvidenceRef(
                            span_id=span.span_id,
                            page=span.page,
                            bbox=span.bbox,
                            artifact_ref=roi.artifact_ref,
                            view=roi.view,
                        )
                    )
            elif roi is not None:
                output.append(roi)
        return output
