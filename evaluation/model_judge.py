from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Protocol, Sequence

from evaluation.judgment_schema import VERDICTS


class JudgeProvider(Protocol):
    model: str

    def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        image_ref: str | list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]: ...


_VERDICT_SCHEMA = {"enum": sorted(VERDICTS)}
INDEPENDENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": _VERDICT_SCHEMA,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "readable": {"type": "boolean"},
        "evidence_pages": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "negative_sign_risk": {"type": "boolean"},
        "summary": {"type": "string"},
    },
    "required": ["verdict", "confidence", "readable", "evidence_pages", "negative_sign_risk", "summary"],
}
CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "candidate_supported": {"type": "boolean"},
        "independent_judge_supported": {"type": "boolean"},
        "proposed_verdict": _VERDICT_SCHEMA,
        "decisive": {"type": "boolean"},
        "evidence_pages": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": [
        "candidate_supported",
        "independent_judge_supported",
        "proposed_verdict",
        "decisive",
        "evidence_pages",
        "reason_codes",
        "summary",
    ],
}
ADJUDICATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": _VERDICT_SCHEMA,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "decisive": {"type": "boolean"},
        "evidence_sufficient": {"type": "boolean"},
        "candidate_supported": {"type": "boolean"},
        "evidence_pages": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": [
        "verdict",
        "confidence",
        "decisive",
        "evidence_sufficient",
        "candidate_supported",
        "evidence_pages",
        "reason_codes",
        "summary",
    ],
}


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def candidate_snapshot_hash(candidate_result: dict[str, Any]) -> str:
    """Stable digest binding a judge row to the exact candidate snapshot."""
    payload = _compact(candidate_result).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validated_payload(value: Any, *, verdict_key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("model judge response must be an object")
    if str(value.get(verdict_key) or "") not in VERDICTS:
        raise ValueError("model judge returned an invalid verdict")
    return value


class MultimodalModelJudge:
    """Three-pass independent judge: blind solve, adversarial critique, adjudication."""

    def __init__(self, provider: JudgeProvider, *, confidence_threshold: float = 0.8) -> None:
        self.provider = provider
        self.confidence_threshold = max(0.5, min(float(confidence_threshold), 1.0))

    def evaluate_question(
        self,
        *,
        assignment_id: str,
        student_hash: str,
        question_id: str,
        candidate_result: dict[str, Any],
        reference: dict[str, Any],
        image_refs: Sequence[str | Path] = (),
        candidate_snapshot: str | None = None,
    ) -> dict[str, Any]:
        safe_images = [str(Path(value).resolve()) for value in image_refs][:2]
        transcription = candidate_result.get("transcription") or []
        reference_payload = {
            "question_id": question_id,
            "question_type": reference.get("question_type", "unknown"),
            "reference_answer": reference.get("reference_answer") or reference.get("problem") or "",
            "rubric_items": reference.get("rubric_items") or [],
            "critical_symbols": reference.get("critical_symbols") or [],
        }

        blind_prompt = (
            "你是独立多模态作业裁判。候选模型的结论被刻意隐藏，先独立判断学生本题。"
            "必须同时核对原图、逐字转写与标准答案；不得按数学合理性补写学生没有写出的负号或步骤。"
            "重点寻找漏识别负号、小数点、等号、分数线、涂改和证明逻辑跳步。"
            "只有原图确实不可辨认时才输出 unreadable。输出严格符合给定 JSON Schema。\n"
            f"reference={_compact(reference_payload)}\n"
            f"student_transcription={_compact(transcription)}"
        )
        independent = _validated_payload(
            self.provider.complete_json(blind_prompt, INDEPENDENT_SCHEMA, image_ref=safe_images or None),
            verdict_key="verdict",
        )

        candidate_view = {
            key: candidate_result.get(key)
            for key in ("verdict", "confidence", "evidence_refs", "rubric_decisions", "needs_verification", "verifier_result")
        }
        critic_prompt = (
            "你是对抗审计员。现在比较候选模型结论和一份独立盲判。你的任务不是折中，而是找出二者"
            "可能共同忽略的视觉符号或逻辑证据。逐项挑战扣分证据，特别复查负号；证据不足必须 decisive=false。"
            "输出严格符合给定 JSON Schema。\n"
            f"reference={_compact(reference_payload)}\n"
            f"student_transcription={_compact(transcription)}\n"
            f"candidate={_compact(candidate_view)}\n"
            f"independent_judge={_compact(independent)}"
        )
        critic = _validated_payload(
            self.provider.complete_json(critic_prompt, CRITIC_SCHEMA, image_ref=safe_images or None),
            verdict_key="proposed_verdict",
        )

        adjudicator_prompt = (
            "你是最终裁决器。根据原图、标准答案、转写、候选结果、独立盲判和对抗审计作最终结论。"
            "不要因多数表决而忽略原图证据；partial/incorrect 必须能定位到证据页。若视觉或逻辑冲突仍未解决，"
            "decisive=false。输出严格符合给定 JSON Schema。\n"
            f"reference={_compact(reference_payload)}\n"
            f"student_transcription={_compact(transcription)}\n"
            f"candidate={_compact(candidate_view)}\n"
            f"independent_judge={_compact(independent)}\n"
            f"critic={_compact(critic)}"
        )
        adjudicator = _validated_payload(
            self.provider.complete_json(adjudicator_prompt, ADJUDICATOR_SCHEMA, image_ref=safe_images or None),
            verdict_key="verdict",
        )

        verdict = str(adjudicator["verdict"])
        confidence = float(adjudicator.get("confidence", 0) or 0)
        evidence_pages = sorted({int(value) for value in adjudicator.get("evidence_pages", []) if int(value) >= 1})
        deduction_has_evidence = verdict not in {"partial", "incorrect"} or bool(evidence_pages)
        scoreable = (
            bool(adjudicator.get("decisive"))
            and bool(adjudicator.get("evidence_sufficient"))
            and confidence >= self.confidence_threshold
            and deduction_has_evidence
        )
        return {
            "schema_version": "1.0",
            "annotation_source": "independent_multimodal_model_judge",
            "annotation_status": "model_confirmed" if scoreable else "model_disputed",
            "assignment_id": assignment_id,
            "student_hash": student_hash,
            "sample_id": f"{assignment_id}:{student_hash}",
            "question_id": question_id,
            "expected_verdict": verdict if scoreable else None,
            "candidate_verdict": candidate_view.get("verdict"),
            "candidate_snapshot_hash": candidate_snapshot or candidate_snapshot_hash(candidate_result),
            "candidate_supported": bool(adjudicator.get("candidate_supported")),
            "judge_confidence": confidence,
            "scoreable": scoreable,
            "evidence_refs": [f"page:{page}" for page in evidence_pages],
            "reason_codes": [str(value) for value in adjudicator.get("reason_codes", [])],
            "judge_summary": str(adjudicator.get("summary", "")),
            "judge_model": str(getattr(self.provider, "model", "unknown")),
            "passes": {
                "independent": independent,
                "critic": critic,
                "adjudicator": adjudicator,
            },
        }
