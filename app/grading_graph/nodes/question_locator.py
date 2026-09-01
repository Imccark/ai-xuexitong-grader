from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from app.grading_graph.schemas import EvidenceRef, QuestionJob


class LocatorProvider(Protocol):
    def complete_json(self, prompt: str, schema: dict[str, Any], image_ref: str | list[str] | None = None) -> dict[str, Any]: ...


LOCATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "found": {"type": "boolean"},
        "locations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer"},
                    "bbox": {"type": "array", "minItems": 4, "maxItems": 4},
                    "confidence": {"type": "number"},
                },
                "required": ["page", "bbox", "confidence"],
            },
        },
        "reason": {"type": "string"},
    },
    "required": ["found", "locations"],
}


def _safe_id(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("_") or "question"


def _pixel_bbox(raw_bbox: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
    if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
        return None
    try:
        values = [float(value) for value in raw_bbox]
    except (TypeError, ValueError):
        return None
    # Locator prompt requests 0..1000 normalized coordinates. Accept literal
    # pixel coordinates too, because compatible providers do not enforce the
    # nested schema consistently.
    if max(values) <= 1000:
        x1, y1, x2, y2 = (
            values[0] * width / 1000,
            values[1] * height / 1000,
            values[2] * width / 1000,
            values[3] * height / 1000,
        )
    else:
        x1, y1, x2, y2 = values
    x1 = max(0, min(width - 1, int(x1)))
    y1 = max(0, min(height - 1, int(y1)))
    x2 = max(x1 + 1, min(width, int(x2)))
    y2 = max(y1 + 1, min(height, int(y2)))
    if (x2 - x1) * (y2 - y1) < max(64, width * height * 0.002):
        return None
    return x1, y1, x2, y2


class QuestionLocator:
    """Answer-blind locator used only after the normal router misses a target."""

    def __init__(self, provider: LocatorProvider) -> None:
        self.provider = provider

    def locate_and_crop(self, job: QuestionJob | dict[str, Any]) -> QuestionJob | None:
        question_job = QuestionJob.model_validate(job)
        page_paths: dict[int, Path] = {}
        for ref in question_job.roi_refs:
            path = Path(ref.artifact_ref)
            if path.is_file() and ref.page not in page_paths:
                page_paths[ref.page] = path
        if not page_paths:
            return None
        aliases = list(question_job.answer_slice.aliases) if question_job.answer_slice else []
        prompt = (
            "你是答案盲的作业题目定位器，只负责在学生作业整页图中寻找指定题号及其连续作答区域。"
            "不得判断答案对错，不得参考标准答案内容。请输出覆盖该题全部书写的最小矩形；若跨页可输出两个位置。"
            "若完整题号缺失、不同页面都出现同名的裸子题号（如仅写(2)），不得擅自只选第一处；"
            "请把所有可能属于目标题的区域分别写入 locations，最多4处，交给后续逐题证据核对。"
            "bbox 必须使用每张图左上为(0,0)、右下为(1000,1000)的归一化坐标。"
            "不要把相邻题目纳入矩形；找不到时 found=false。输出严格 JSON。\n"
            f"question_id={question_job.question_id}\n"
            f"aliases={json.dumps(aliases, ensure_ascii=False)}\n"
            f"available_pages={sorted(page_paths)}"
        )
        image_refs = [str(page_paths[page]) for page in sorted(page_paths)[:4]]
        payload = self.provider.complete_json(prompt, LOCATOR_SCHEMA, image_ref=image_refs)
        if not isinstance(payload, dict) or not bool(payload.get("found")):
            return None
        raw_locations = payload.get("locations") or []
        if isinstance(raw_locations, dict):
            raw_locations = [raw_locations]
        crops: list[EvidenceRef] = []
        for index, item in enumerate(raw_locations, 1):
            if not isinstance(item, dict):
                continue
            try:
                page = int(item.get("page"))
                # Qwen JSON mode sometimes omits nested optional/required
                # fields even when the top-level ``found`` is explicit.
                # A valid page+bbox is still useful; keep it high-risk and
                # let the downstream grader/verifier decide correctness.
                confidence = float(item.get("confidence", 0.8) or 0.8)
            except (TypeError, ValueError):
                continue
            source = page_paths.get(page)
            if source is None or confidence < 0.5:
                continue
            with Image.open(source) as image:
                image.load()
                box = _pixel_bbox(item.get("bbox"), image.width, image.height)
                if box is None:
                    continue
                problem = question_job.answer_slice.problem if question_job.answer_slice else ""
                explicit_subparts = set(re.findall(r"\\textbf\{\((\d+)\)\}", problem))
                if len(explicit_subparts) >= 2:
                    # Compound answers commonly continue far below the first
                    # located ``(1)/(2)`` marker. Preserve the entire vertical
                    # tail so a small locator box cannot crop off the final
                    # equation or conclusion.
                    box = (0, max(0, box[1] - int(image.height * 0.05)), image.width, image.height)
                crop = image.crop(box)
                crop_path = source.with_name(f"located_{_safe_id(question_job.question_id)}_{index}.png")
                temporary = crop_path.with_suffix(".tmp.png")
                crop.save(temporary, format="PNG")
                temporary.replace(crop_path)
                crops.append(
                    EvidenceRef(
                        span_id=f"located-{_safe_id(question_job.question_id)}-{index}",
                        page=page,
                        bbox=(0, 0, crop.width, crop.height),
                        artifact_ref=str(crop_path),
                        view="normalized",
                    )
                )
        if not crops:
            return None
        return question_job.model_copy(
            update={
                "pages": sorted({ref.page for ref in crops}),
                "roi_refs": crops[:4],
                "route": "risk",
            }
        )
