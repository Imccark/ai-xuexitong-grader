from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Protocol


class PageObservationProvider(Protocol):
    def complete_json(self, prompt: str, schema: dict[str, Any], image_ref: str | None = None) -> dict[str, Any]: ...


class PageObservationError(ValueError):
    pass


PAGE_OBSERVATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "page_type": {"enum": ["assignment", "cover", "blank", "wrong_subject", "unknown"]},
        "rotation_degrees_clockwise": {"type": "integer", "enum": [0, 90, 180, 270]},
        "orientation_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question_id", "bbox", "confidence"],
                "properties": {
                    "question_id": {"type": "string"},
                    "bbox": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "integer"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "question_type": {"type": "string"},
                    "artifact_ref": {"type": "string"},
                },
            },
        },
    },
    "required": ["page_type", "questions"],
}


def build_page_observation_prompt(page: int) -> str:
    return (
        "你是页面和题号观察器。只观察当前学生作业图片，识别页面类型、可见题号和每道题的区域。"
        "图片通常已经转正；若仍然横置或倒置，请报告使文字便于阅读所需的顺时针旋转角度及置信度，否则填 0。"
        "不要读取或猜测标准答案，不判断学生答案正误；看不清的题号必须降低 confidence 或省略。"
        "输出严格 JSON。\n"
        f"page={page}"
    )


def _bbox(raw: Any) -> tuple[int, int, int, int]:
    if isinstance(raw, dict):
        raw = [raw.get("x_min"), raw.get("y_min"), raw.get("x_max"), raw.get("y_max")]
    if isinstance(raw, str):
        raw = [part for part in re.split(r"[\s,;，；]+", raw.strip()) if part]
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise PageObservationError("question bbox must contain four integers")
    try:
        values = tuple(int(value) for value in raw)
    except (TypeError, ValueError) as exc:
        raise PageObservationError("question bbox must contain four integers") from exc
    x1, y1, x2, y2 = values
    if min(values) < 0 or x2 <= x1 or y2 <= y1:
        raise PageObservationError("question bbox is invalid")
    return values


class PageObserver:
    def __init__(self, provider: PageObservationProvider) -> None:
        self.provider = provider

    def observe(self, page_path: Path | str, *, page: int) -> dict[str, Any]:
        payload = self.provider.complete_json(
            build_page_observation_prompt(page), PAGE_OBSERVATION_SCHEMA, image_ref=str(page_path)
        )
        if not isinstance(payload, dict):
            raise PageObservationError("page observation must be an object")
        page_type = str(payload.get("page_type", "unknown")).strip().lower()
        # Qwen has returned the legacy ``homework/problems/id`` shape for
        # cached responses. Normalize it at the provider boundary so a schema
        # alias cannot turn an otherwise readable page into ``unknown``.
        page_type = {
            "homework": "assignment",
            "assignment_page": "assignment",
        "handwritten_homework": "assignment",
        "homework_solution": "assignment",
            "homework_notebook": "assignment",
            "handwritten_notes": "assignment",
            "handwritten_notebook": "assignment",
        }.get(page_type, page_type)
        if page_type not in {"assignment", "cover", "blank", "wrong_subject", "unknown"}:
            raise PageObservationError("invalid page_type")
        raw_questions = payload.get("questions")
        if raw_questions is None:
            raw_questions = payload.get("problems")
        if raw_questions is None:
            raw_questions = payload.get("question_regions")
        if raw_questions is None:
            raw_questions = payload.get("visible_questions")
        if not isinstance(raw_questions, list):
            raise PageObservationError("page observation questions must be a list")
        questions: list[dict[str, Any]] = []
        for raw in raw_questions:
            if not isinstance(raw, dict):
                raise PageObservationError("page observation question must be an object")
            question_id = " ".join(
                str(
                    raw.get("question_id")
                    or raw.get("id")
                    or raw.get("question_number")
                    or raw.get("number")
                    or ""
                ).split()
            )
            if not question_id:
                # A page-level observation can still be useful when one region
                # is malformed; discard only that region instead of losing the
                # entire page and forcing every question to unreadable.
                continue
            confidence = float(raw.get("confidence", 0))
            if not 0 <= confidence <= 1:
                continue
            try:
                bbox = list(_bbox(raw.get("bbox") or raw.get("bbox_2d") or raw.get("region")))
            except PageObservationError:
                continue
            questions.append(
                {
                    "question_id": question_id,
                    "bbox": bbox,
                    "confidence": confidence,
                    "question_type": str(raw.get("question_type") or "unknown"),
                    # The model may describe a region, but it must not choose a
                    # filesystem path that a later node will open.
                    "artifact_ref": str(Path(page_path).resolve()),
                }
            )
        rotation = int(payload.get("rotation_degrees_clockwise", 0) or 0)
        if rotation not in {0, 90, 180, 270}:
            rotation = 0
        orientation_confidence = float(payload.get("orientation_confidence", 0.0) or 0.0)
        if not 0 <= orientation_confidence <= 1:
            orientation_confidence = 0.0
        return {
            "page": page,
            "page_type": page_type,
            "rotation_degrees_clockwise": rotation,
            "orientation_confidence": orientation_confidence,
            "questions": questions,
        }
