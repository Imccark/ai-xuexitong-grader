from __future__ import annotations

import json
from typing import Any, Protocol, Sequence

from app.grading_graph.nodes.grader import GradingProviderError
from app.grading_graph.schemas import SymbolCandidate, TranscriptionSpan


class SymbolAuditProvider(Protocol):
    def complete_json(self, prompt: str, schema: dict[str, Any], image_ref: str | list[str] | None = None) -> dict[str, Any]: ...


SYMBOL_AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "symbol_candidates": {"type": "array"},
        "decisive": {"type": "boolean"},
        "reason": {"type": "string"},
        "corrected_text": {"type": "string"},
    },
    "required": ["symbol_candidates", "decisive", "reason"],
}


class SymbolAuditor:
    def __init__(self, provider: SymbolAuditProvider, *, max_rounds: int = 2) -> None:
        self.provider = provider
        self.max_rounds = max(1, min(max_rounds, 2))

    def audit(self, span: TranscriptionSpan, *, image_ref: str | list[str] | None = None) -> TranscriptionSpan:
        prompt = (
            "你是 symbol auditor，只做原图逐字复核，不解题。输入图通常已经按 span 裁剪并放大；"
            "此时直接逐字检查整张局部图，bbox 只用于来源审计。逐个清点 "
            "minus、blank、equals、fraction_bar、erasure，再把该 span 原样重抄到 corrected_text。"
            "不得按数学合理性补笔画；转写与原图冲突时以原图为准，无法确定就保留 unknown 并令 decisive=false。"
            "尤其要核对通解常数项、参数系数前的负号，以及结尾是否还有一行等式。输出严格 JSON。\n"
            f"span={json.dumps(span.model_dump(mode='json'), ensure_ascii=False)}"
        )
        last_candidates: list[SymbolCandidate] = []
        last_error_type: str | None = None
        for round_index in range(self.max_rounds):
            try:
                payload = self.provider.complete_json(prompt, SYMBOL_AUDIT_SCHEMA, image_ref=image_ref)
                if not isinstance(payload, dict):
                    raise ValueError("symbol audit response must be an object")
                raw_candidates = payload.get("symbol_candidates", payload.get("symbols", [])) if isinstance(payload, dict) else []
                normalized_candidates: list[dict[str, Any]] = []
                for item in raw_candidates if isinstance(raw_candidates, list) else []:
                    if isinstance(item, dict):
                        symbol = item.get("symbol")
                        if symbol not in {"minus", "blank", "equals", "fraction_bar", "erasure", "unknown"}:
                            kind = str(item.get("type") or "")
                            char = item.get("char")
                            symbol = "minus" if kind == "minus" or char in {"-", "−"} else "equals" if kind == "equals" or char == "=" else "fraction_bar" if kind in {"fraction_bar", "determinant_bar"} else "unknown"
                        normalized_candidates.append({"symbol": symbol, "confidence": float(item.get("confidence", span.confidence) or span.confidence)})
                    elif isinstance(item, str):
                        symbol = "minus" if item in {"-", "−"} else "equals" if item == "=" else "fraction_bar" if "/" in item else "unknown"
                        normalized_candidates.append({"symbol": symbol, "confidence": span.confidence})
                last_candidates = [SymbolCandidate.model_validate(item) for item in normalized_candidates]
            except Exception as exc:
                # Transient 429/5xx/timeouts are common when several question
                # nodes fan out at once. Consume the bounded audit rounds
                # before escalating so one flaky request does not poison the
                # whole student's batch.
                last_error_type = type(exc).__name__
                continue
            if bool(payload.get("decisive")):
                if not last_candidates:
                    last_candidates = [SymbolCandidate(symbol="unknown", confidence=1.0)]
                corrected_text = str(payload.get("corrected_text") or "").strip()
                return span.model_copy(
                    update={
                        "text": corrected_text or span.text,
                        "symbol_candidates": last_candidates,
                        "readability": "clear",
                    }
                )
        if last_error_type and not last_candidates:
            raise GradingProviderError(
                f"symbol audit failed for {span.span_id} ({last_error_type})"
            ) from None
        if not last_candidates:
            last_candidates = [SymbolCandidate(symbol="unknown", confidence=1.0)]
        return span.model_copy(update={"symbol_candidates": last_candidates, "readability": "uncertain"})
