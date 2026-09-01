from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from grading_graph.schemas import EvidenceRef, TranscriptionSpan


class TranscriptionProvider(Protocol):
    def complete_json(self, prompt: str, schema: dict[str, Any], image_ref: str | None = None) -> dict[str, Any]: ...


class TranscriptionProviderError(ValueError):
    pass


TRANSCRIPTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "spans": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["span_id", "page", "bbox", "text", "symbol_candidates", "readability", "confidence"],
                "properties": {
                    "span_id": {"type": "string"},
                    "page": {"type": "integer", "minimum": 1},
                    "bbox": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "integer"}},
                    "text": {"type": "string"},
                    "symbol_candidates": {"type": "array"},
                    "readability": {"enum": ["clear", "uncertain", "unreadable"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
    "required": ["spans"],
}


def build_literal_prompt(page: int, roi_refs: list[dict[str, Any] | EvidenceRef]) -> str:
    rois = []
    for ref in roi_refs:
        if isinstance(ref, EvidenceRef):
            rois.append({"span_id": ref.span_id, "page": ref.page, "bbox": list(ref.bbox)})
        else:
            rois.append({"span_id": ref.get("span_id", "roi"), "page": page, "bbox": ref.get("bbox")})
    return (
        "你是忠实转写器。只报告图片中可见的学生书写，不根据数学合理性补全。"
        "不确定字符输出候选或 unknown；必须区分负号、空白、等号、分数线和涂改线。"
        "输出严格 JSON，包含每个可见行的页码、bbox、文本、符号候选和置信度；不要判断正误。\n"
        f"当前页：{page}\nROI：{json.dumps(rois, ensure_ascii=False)}"
    )


class LiteralTranscriber:
    def __init__(self, provider: TranscriptionProvider) -> None:
        self.provider = provider

    @staticmethod
    def _normalize_legacy_span(raw: dict[str, Any], *, page: int, index: int) -> dict[str, Any]:
        """Adapt Qwen's compact OCR span shape to the strict graph schema."""
        value = dict(raw)
        if "bbox" not in value:
            value["bbox"] = value.get("bbox_2d") or value.get("region") or value.get("box")
        if "text" not in value:
            value["text"] = value.get("content") or value.get("transcription") or value.get("ocr_text") or ""
        value["span_id"] = str(value.get("span_id") or f"p{page}-span-{index + 1}")
        # A request contains one image page; the requested page is authoritative
        # even when the model echoes an incorrect page number.
        value["page"] = page
        confidence = float(value.get("confidence", 0.0) or 0.0)
        confidence = max(0.0, min(1.0, confidence))
        value["confidence"] = confidence
        value.setdefault(
            "readability",
            "clear" if confidence >= 0.85 else "uncertain" if confidence >= 0.55 else "unreadable",
        )
        readability = str(value.get("readability") or "").strip().lower()
        value["readability"] = {
            "readable": "clear",
            "clear": "clear",
            "unclear": "uncertain",
            "uncertain": "uncertain",
            "illegible": "unreadable",
            "unreadable": "unreadable",
        }.get(readability, "uncertain")
        candidates = value.get("symbol_candidates", value.get("symbols", []))
        value.pop("symbols", None)
        if isinstance(candidates, dict):
            normalized: list[dict[str, Any]] = []
            for glyph in candidates:
                glyph_text = str(glyph)
                symbol = "unknown"
                if "-" in glyph_text or "−" in glyph_text:
                    symbol = "minus"
                elif "=" in glyph_text:
                    symbol = "equals"
                elif "/" in glyph_text:
                    symbol = "fraction_bar"
                normalized.append({"symbol": symbol, "confidence": confidence})
            value["symbol_candidates"] = normalized
        elif isinstance(candidates, list):
            normalized = []
            for candidate in candidates:
                if isinstance(candidate, dict):
                    symbol = candidate.get("symbol")
                    if symbol not in {"minus", "blank", "equals", "fraction_bar", "erasure", "unknown"}:
                        kind = str(candidate.get("type") or "")
                        if kind not in {"minus", "equals", "fraction_bar", "determinant_bar"} and candidate.get("char") not in {"-", "−", "="}:
                            continue
                        symbol = "minus" if kind == "minus" or candidate.get("char") in {"-", "−"} else "equals" if kind == "equals" or candidate.get("char") == "=" else "fraction_bar" if kind in {"fraction_bar", "determinant_bar"} else "unknown"
                    if symbol in {"minus", "blank", "equals", "fraction_bar", "erasure", "unknown"}:
                        normalized.append({"symbol": symbol, "confidence": float(candidate.get("confidence", confidence) or confidence)})
                elif isinstance(candidate, str):
                    glyph_text = candidate
                    symbol = "minus" if ("-" in glyph_text or "−" in glyph_text) else "equals" if "=" in glyph_text else "fraction_bar" if "/" in glyph_text else "unknown"
                    normalized.append({"symbol": symbol, "confidence": confidence})
            value["symbol_candidates"] = normalized
        else:
            value["symbol_candidates"] = []
        return value

    def transcribe(
        self,
        page_path: Path | str,
        *,
        page: int,
        roi_refs: list[dict[str, Any] | EvidenceRef],
    ) -> list[TranscriptionSpan]:
        prompt = build_literal_prompt(page, roi_refs)
        payload = self.provider.complete_json(prompt, TRANSCRIPTION_SCHEMA, image_ref=str(page_path))
        if not isinstance(payload, dict) or not isinstance(payload.get("spans"), list):
            raise TranscriptionProviderError("transcriber response must contain spans")
        spans: list[TranscriptionSpan] = []
        for index, raw in enumerate(payload["spans"]):
            if not isinstance(raw, dict):
                raise TranscriptionProviderError("transcriber span must be an object")
            raw = self._normalize_legacy_span(raw, page=page, index=index)
            try:
                span = TranscriptionSpan.model_validate(raw)
            except ValueError as exc:
                failure_kind = "bbox" if "bbox" in str(exc) else "schema validation"
                raise TranscriptionProviderError(
                    f"transcriber span failed {failure_kind}"
                ) from None
            spans.append(span)
        return spans
