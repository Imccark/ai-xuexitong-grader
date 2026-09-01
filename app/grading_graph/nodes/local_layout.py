from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from PIL import Image

from app.grading_graph.schemas import AnswerManifest


CONTENT_REGION_TYPES = frozenset(
    {"question_block", "subquestion", "student_answer", "cross_page_continuation", "unknown"}
)
IGNORED_REGION_TYPES = frozenset({"identity", "header_footer"})
SUPPORTED_REGION_TYPES = CONTENT_REGION_TYPES | IGNORED_REGION_TYPES
DEFAULT_LAYOUT_CONTENT_LABELS = frozenset(
    {
        "text",
        "paragraph_title",
        "inline_formula",
        "display_formula",
        "number",
        "list",
        "algorithm",
        "formula_number",
    }
)


class LocalLayoutUnavailable(RuntimeError):
    """Raised when the optional local layout runtime cannot be used safely."""


class LocalLayoutBackend(Protocol):
    def predict(self, image_path: Path | str) -> Iterable[Any]: ...


class QuestionLabelReader(Protocol):
    def read(self, image_path: Path | str, bbox: tuple[int, int, int, int]) -> list[tuple[str, float]]: ...

    def read_page(self, image_path: Path | str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class LocalLayoutSettings:
    enabled: bool = False
    backend: str = "paddleocr"
    model_name: str = "PP-DocLayoutV3"
    model_dir: Path | None = None
    engine: str = "onnxruntime"
    allow_model_download: bool = False
    min_region_confidence: float = 0.80
    min_question_label_confidence: float = 0.85
    min_region_area_ratio: float = 0.002
    max_sibling_overlap_ratio: float = 0.85
    question_id_ocr_enabled: bool = False
    ocr_language: str = "ch"
    ocr_det_model_name: str = "PP-OCRv5_mobile_det"
    ocr_rec_model_name: str = "PP-OCRv5_mobile_rec"
    ocr_det_model_dir: Path | None = None
    ocr_rec_model_dir: Path | None = None
    ocr_left_strip_ratio: float = 0.45
    ocr_text_det_limit_side_len: int = 736
    ocr_enable_mkldnn: bool = False
    merge_default_regions: bool = True
    merge_gap_height_multiplier: float = 1.5
    merge_gap_min_page_ratio: float = 0.018
    merge_gap_max_page_ratio: float = 0.08
    merge_horizontal_padding_ratio: float = 0.035
    merge_vertical_padding_ratio: float = 0.025
    label_map: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        base_dir: Path | str | None = None,
    ) -> "LocalLayoutSettings":
        payload = dict(value or {})
        root = Path(base_dir or Path.cwd()).resolve()

        def optional_path(key: str) -> Path | None:
            raw = str(payload.get(key) or "").strip()
            if not raw:
                return None
            path = Path(raw)
            return (path if path.is_absolute() else root / path).resolve()

        settings = cls(
            enabled=bool(payload.get("enabled", False)),
            backend=str(payload.get("backend") or "paddleocr"),
            model_name=str(payload.get("model_name") or "PP-DocLayoutV3"),
            model_dir=optional_path("model_dir"),
            engine=str(payload.get("engine") or "onnxruntime"),
            allow_model_download=bool(payload.get("allow_model_download", False)),
            min_region_confidence=float(payload.get("min_region_confidence", 0.80)),
            min_question_label_confidence=float(payload.get("min_question_label_confidence", 0.85)),
            min_region_area_ratio=float(payload.get("min_region_area_ratio", 0.002)),
            max_sibling_overlap_ratio=float(payload.get("max_sibling_overlap_ratio", 0.85)),
            question_id_ocr_enabled=bool(payload.get("question_id_ocr_enabled", False)),
            ocr_language=str(payload.get("ocr_language") or "ch"),
            ocr_det_model_name=str(payload.get("ocr_det_model_name") or "PP-OCRv5_mobile_det"),
            ocr_rec_model_name=str(payload.get("ocr_rec_model_name") or "PP-OCRv5_mobile_rec"),
            ocr_det_model_dir=optional_path("ocr_det_model_dir"),
            ocr_rec_model_dir=optional_path("ocr_rec_model_dir"),
            ocr_left_strip_ratio=float(payload.get("ocr_left_strip_ratio", 0.45)),
            ocr_text_det_limit_side_len=int(payload.get("ocr_text_det_limit_side_len", 736)),
            ocr_enable_mkldnn=bool(payload.get("ocr_enable_mkldnn", False)),
            merge_default_regions=bool(payload.get("merge_default_regions", True)),
            merge_gap_height_multiplier=float(payload.get("merge_gap_height_multiplier", 1.5)),
            merge_gap_min_page_ratio=float(payload.get("merge_gap_min_page_ratio", 0.018)),
            merge_gap_max_page_ratio=float(payload.get("merge_gap_max_page_ratio", 0.08)),
            merge_horizontal_padding_ratio=float(payload.get("merge_horizontal_padding_ratio", 0.035)),
            merge_vertical_padding_ratio=float(payload.get("merge_vertical_padding_ratio", 0.025)),
            label_map={str(key): str(item) for key, item in dict(payload.get("label_map") or {}).items()},
        )
        for name, number in (
            ("min_region_confidence", settings.min_region_confidence),
            ("min_question_label_confidence", settings.min_question_label_confidence),
            ("min_region_area_ratio", settings.min_region_area_ratio),
            ("max_sibling_overlap_ratio", settings.max_sibling_overlap_ratio),
            ("ocr_left_strip_ratio", settings.ocr_left_strip_ratio),
            ("merge_gap_min_page_ratio", settings.merge_gap_min_page_ratio),
            ("merge_gap_max_page_ratio", settings.merge_gap_max_page_ratio),
            ("merge_horizontal_padding_ratio", settings.merge_horizontal_padding_ratio),
            ("merge_vertical_padding_ratio", settings.merge_vertical_padding_ratio),
        ):
            if not 0 <= number <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if settings.engine not in {"paddle_static", "paddle_dynamic", "transformers", "onnxruntime"}:
            raise ValueError("unsupported local layout engine")
        if settings.backend != "paddleocr":
            raise ValueError("unsupported local layout backend")
        if settings.ocr_text_det_limit_side_len < 64:
            raise ValueError("ocr_text_det_limit_side_len must be at least 64")
        if settings.merge_gap_height_multiplier <= 0:
            raise ValueError("merge_gap_height_multiplier must be positive")
        if settings.merge_gap_min_page_ratio > settings.merge_gap_max_page_ratio:
            raise ValueError("merge gap minimum cannot exceed maximum")
        return settings

    def audit_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "model_name": self.model_name,
            "engine": self.engine,
            "model_dir": str(self.model_dir) if self.model_dir else None,
            "allow_model_download": self.allow_model_download,
            "min_region_confidence": self.min_region_confidence,
            "min_question_label_confidence": self.min_question_label_confidence,
            "question_id_ocr_enabled": self.question_id_ocr_enabled,
            "merge_default_regions": self.merge_default_regions,
        }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    json_value = getattr(value, "json", None)
    if callable(json_value):
        json_value = json_value()
    if isinstance(json_value, Mapping):
        return json_value
    res_value = getattr(value, "res", None)
    if isinstance(res_value, Mapping):
        return {"res": res_value}
    data = getattr(value, "__dict__", None)
    if isinstance(data, Mapping):
        return data
    raise ValueError("local layout result is not mapping-compatible")


def _result_boxes(value: Any) -> list[Mapping[str, Any]]:
    payload = _as_mapping(value)
    nested = payload.get("res")
    if isinstance(nested, Mapping):
        payload = nested
    boxes = payload.get("boxes") or payload.get("regions") or payload.get("layout") or []
    if not isinstance(boxes, list):
        raise ValueError("local layout result boxes must be a list")
    return [item for item in boxes if isinstance(item, Mapping)]


def _bbox_from_item(item: Mapping[str, Any], width: int, height: int) -> tuple[int, int, int, int] | None:
    raw = item.get("coordinate") or item.get("bbox") or item.get("box")
    if raw is None:
        raw = item.get("polygon_points") or item.get("polygon")
    if isinstance(raw, list) and raw and isinstance(raw[0], (list, tuple)):
        points = [point for point in raw if isinstance(point, (list, tuple)) and len(point) >= 2]
        if not points:
            return None
        raw = [
            min(float(point[0]) for point in points),
            min(float(point[1]) for point in points),
            max(float(point[0]) for point in points),
            max(float(point[1]) for point in points),
        ]
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        values = [float(number) for number in raw]
    except (TypeError, ValueError):
        return None
    # Custom exports may use normalized xyxy while PaddleOCR returns pixels.
    if max(values) <= 1.0:
        values = [values[0] * width, values[1] * height, values[2] * width, values[3] * height]
    x1 = max(0, min(width - 1, int(values[0])))
    y1 = max(0, min(height - 1, int(values[1])))
    x2 = max(x1 + 1, min(width, int(values[2])))
    y2 = max(y1 + 1, min(height, int(values[3])))
    return x1, y1, x2, y2


def normalize_question_label(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = normalized.translate(
        str.maketrans({"（": "(", "）": ")", "．": ".", "。": ".", "，": ",", "、": "."})
    )
    normalized = re.sub(r"\s+", "", normalized)
    normalized = re.sub(r"^[第题号:：]+|[题号:：]+$", "", normalized)
    return normalized.rstrip(".,")


def question_label_candidates(text: str) -> list[str]:
    normalized = normalize_question_label(text)
    candidates: list[str] = []
    patterns = (
        r"\d+(?:\.\d+){1,4}(?:\(\d+\))?",
        r"\d+(?:\(\d+\))",
        r"\(\d+\)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, normalized):
            value = match.group(0)
            if value not in candidates:
                candidates.append(value)
    if normalized and normalized not in candidates:
        candidates.append(normalized)
    return candidates


def _alias_index(manifest: AnswerManifest) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for question_id, answer_slice in manifest.questions.items():
        for raw_alias in [question_id, *answer_slice.aliases]:
            alias = normalize_question_label(raw_alias)
            if alias:
                index.setdefault(alias, set()).add(question_id)
    return index


def resolve_question_label(text: str, manifest: AnswerManifest) -> str | None:
    index = _alias_index(manifest)
    matches: set[str] = set()
    candidates = question_label_candidates(text)
    for candidate in candidates:
        matches.update(index.get(normalize_question_label(candidate), set()))
    if len(matches) == 1:
        return next(iter(matches))
    if matches:
        return None
    # Handwritten OCR often drops one separator (``24.5`` for ``2.4.5``).
    # The answer manifest makes a numeric-signature repair safe only when the
    # repaired form maps to exactly one configured question.
    signature_index: dict[str, set[str]] = {}
    for alias, question_ids in index.items():
        signature = re.sub(r"\D", "", alias)
        if signature:
            signature_index.setdefault(signature, set()).update(question_ids)
    for candidate in candidates:
        signature = re.sub(r"\D", "", candidate)
        if signature:
            matches.update(signature_index.get(signature, set()))
    return next(iter(matches)) if len(matches) == 1 else None


class PaddleLayoutBackend:
    """Lazy PaddleOCR adapter. It never downloads a model unless explicitly allowed."""

    def __init__(self, settings: LocalLayoutSettings) -> None:
        self.settings = settings
        self._model: Any = None
        self._initialization_error: Exception | None = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if self._initialization_error is not None:
            raise LocalLayoutUnavailable(type(self._initialization_error).__name__)
        try:
            if self.settings.model_dir is None and not self.settings.allow_model_download:
                raise LocalLayoutUnavailable("local PP-DocLayoutV3 model_dir is required")
            if self.settings.model_dir is not None and not self.settings.model_dir.is_dir():
                raise LocalLayoutUnavailable("local PP-DocLayoutV3 model_dir does not exist")
            from paddleocr import LayoutDetection

            kwargs: dict[str, Any] = {
                "model_name": self.settings.model_name,
                "engine": self.settings.engine,
            }
            if self.settings.model_dir is not None:
                kwargs["model_dir"] = str(self.settings.model_dir)
            self._model = LayoutDetection(**kwargs)
            return self._model
        except Exception as exc:
            self._initialization_error = exc
            if isinstance(exc, LocalLayoutUnavailable):
                raise
            raise LocalLayoutUnavailable(type(exc).__name__) from exc

    def predict(self, image_path: Path | str) -> Iterable[Any]:
        model = self._load()
        return model.predict(str(Path(image_path).resolve()), batch_size=1, layout_nms=True)


class PaddleOCRQuestionLabelReader:
    """Optional local OCR reader restricted to a small label band in each ROI."""

    def __init__(self, settings: LocalLayoutSettings) -> None:
        self.settings = settings
        self._model: Any = None
        self._initialization_error: Exception | None = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if self._initialization_error is not None:
            raise LocalLayoutUnavailable(type(self._initialization_error).__name__)
        try:
            if not self.settings.question_id_ocr_enabled:
                raise LocalLayoutUnavailable("local question-id OCR is disabled")
            if not self.settings.allow_model_download:
                if self.settings.ocr_det_model_dir is None or self.settings.ocr_rec_model_dir is None:
                    raise LocalLayoutUnavailable("local OCR model directories are required")
                if not self.settings.ocr_det_model_dir.is_dir() or not self.settings.ocr_rec_model_dir.is_dir():
                    raise LocalLayoutUnavailable("local OCR model directory does not exist")
            from paddleocr import PaddleOCR

            kwargs: dict[str, Any] = {
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
                "enable_mkldnn": self.settings.ocr_enable_mkldnn,
                "text_det_limit_side_len": self.settings.ocr_text_det_limit_side_len,
                "text_det_limit_type": "max",
                "text_detection_model_name": self.settings.ocr_det_model_name,
                "text_recognition_model_name": self.settings.ocr_rec_model_name,
            }
            if self.settings.ocr_det_model_dir is not None:
                kwargs["text_detection_model_dir"] = str(self.settings.ocr_det_model_dir)
            if self.settings.ocr_rec_model_dir is not None:
                kwargs["text_recognition_model_dir"] = str(self.settings.ocr_rec_model_dir)
            if self.settings.ocr_det_model_dir is None and self.settings.ocr_rec_model_dir is None:
                kwargs["lang"] = self.settings.ocr_language
            self._model = PaddleOCR(**kwargs)
            return self._model
        except Exception as exc:
            self._initialization_error = exc
            if isinstance(exc, LocalLayoutUnavailable):
                raise
            raise LocalLayoutUnavailable(type(exc).__name__) from exc

    def _predict(self, crop: Image.Image) -> list[dict[str, Any]]:
        model = self._load()
        try:
            import numpy as np

            outputs = model.predict(np.asarray(crop.convert("RGB")))
        except Exception as exc:
            raise LocalLayoutUnavailable(type(exc).__name__) from exc
        candidates: list[dict[str, Any]] = []
        for output in outputs:
            payload = _as_mapping(output)
            nested = payload.get("res")
            if isinstance(nested, Mapping):
                payload = nested
            texts = payload.get("rec_texts")
            if texts is None:
                texts = payload.get("texts")
            scores = payload.get("rec_scores")
            if scores is None:
                scores = payload.get("scores")
            boxes = payload.get("rec_boxes")
            if boxes is None:
                boxes = payload.get("boxes")
            texts = [] if texts is None else texts
            scores = [] if scores is None else scores
            boxes = [] if boxes is None else boxes
            if hasattr(texts, "tolist"):
                texts = texts.tolist()
            if hasattr(scores, "tolist"):
                scores = scores.tolist()
            if hasattr(boxes, "tolist"):
                boxes = boxes.tolist()
            if isinstance(texts, str):
                texts = [texts]
            for index, text in enumerate(texts if isinstance(texts, list) else []):
                try:
                    score = float(scores[index]) if index < len(scores) else 0.0
                except (TypeError, ValueError):
                    score = 0.0
                bbox = boxes[index] if index < len(boxes) else None
                candidates.append({"text": str(text), "confidence": score, "bbox": bbox})
        return candidates

    def read(self, image_path: Path | str, bbox: tuple[int, int, int, int]) -> list[tuple[str, float]]:
        with Image.open(image_path) as image:
            image.load()
            x1, y1, x2, y2 = bbox
            # Question labels normally live near the upper-left edge. Keep a
            # generous band so handwritten labels are not clipped, but avoid
            # sending the full mathematical solution through OCR.
            band_width = max(96, int((x2 - x1) * 0.45))
            band_height = max(72, int((y2 - y1) * 0.35))
            crop = image.crop((x1, y1, min(x2, x1 + band_width), min(y2, y1 + band_height))).convert("RGB")
        return [(item["text"], float(item["confidence"])) for item in self._predict(crop)]

    def read_page(self, image_path: Path | str) -> list[dict[str, Any]]:
        with Image.open(image_path) as image:
            image.load()
            strip_width = min(
                image.width,
                max(240, int(round(image.width * self.settings.ocr_left_strip_ratio))),
            )
            strip = image.crop((0, 0, strip_width, image.height)).convert("RGB")
        anchors: list[dict[str, Any]] = []
        for item in self._predict(strip):
            raw_bbox = item.get("bbox")
            if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
                continue
            try:
                bbox = [int(round(float(value))) for value in raw_bbox]
            except (TypeError, ValueError):
                continue
            anchors.append(
                {
                    "text": str(item["text"]),
                    "confidence": float(item["confidence"]),
                    "bbox": bbox,
                }
            )
        return anchors


def _intersection_over_smaller(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> float:
    intersection = max(0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0, min(left[3], right[3]) - max(left[1], right[1])
    )
    left_area = max(1, (left[2] - left[0]) * (left[3] - left[1]))
    right_area = max(1, (right[2] - right[0]) * (right[3] - right[1]))
    return intersection / min(left_area, right_area)


def _default_content_regions(
    raw_boxes: list[Mapping[str, Any]],
    *,
    width: int,
    height: int,
    page: int,
    settings: LocalLayoutSettings,
) -> tuple[list[dict[str, Any]], list[str]]:
    regions: list[dict[str, Any]] = []
    reasons: list[str] = []
    for index, item in enumerate(raw_boxes, start=1):
        raw_label = str(item.get("label") or item.get("class_name") or "unknown")
        try:
            score = float(item.get("score", item.get("confidence", 0.0)) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        bbox = _bbox_from_item(item, width, height)
        is_content = raw_label in DEFAULT_LAYOUT_CONTENT_LABELS
        # The default model occasionally labels a handwritten problem heading
        # as a header. Only keep such a box when it is away from the page head.
        if raw_label == "header" and bbox is not None and bbox[1] > height * 0.08:
            is_content = True
        if not is_content:
            continue
        if bbox is None:
            reasons.append("invalid_content_bbox")
            continue
        area_ratio = ((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / max(1, width * height)
        if score < settings.min_region_confidence or area_ratio < settings.min_region_area_ratio:
            continue
        regions.append(
            {
                "region_id": f"local-p{page}-raw-{index}",
                "region_type": raw_label,
                "bbox": list(bbox),
                "score": round(score, 6),
                "reading_order": int(item.get("reading_order", item.get("order", index - 1)) or 0),
                "question_label": "",
                "question_label_confidence": 0.0,
                "question_id": None,
            }
        )
    regions.sort(key=lambda item: (item["bbox"][1], item["bbox"][0], item["region_id"]))
    return regions, reasons


def _resolved_page_anchors(
    label_reader: QuestionLabelReader | None,
    image_path: Path,
    manifest: AnswerManifest,
    settings: LocalLayoutSettings,
    *,
    width: int,
    height: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    if label_reader is None:
        return [], [], ["question_id_ocr_unavailable"]
    read_page = getattr(label_reader, "read_page", None)
    if not callable(read_page):
        return [], [], ["question_id_ocr_unavailable"]
    try:
        raw_anchors = list(read_page(image_path))
    except LocalLayoutUnavailable:
        return [], [], ["question_id_ocr_unavailable"]
    resolved: list[dict[str, Any]] = []
    audit_anchors: list[dict[str, Any]] = []
    reasons: list[str] = []
    for index, item in enumerate(raw_anchors, start=1):
        text = str(item.get("text") or "").strip()
        try:
            confidence = float(item.get("confidence", item.get("score", 0.0)) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        bbox = _bbox_from_item(item, width, height)
        candidate_question_id = resolve_question_label(text, manifest)
        if candidate_question_id is None:
            # Do not persist arbitrary left-strip OCR text in the layout audit.
            # Only recognized manifest question identifiers are relevant here.
            continue
        question_id = (
            candidate_question_id
            if confidence >= settings.min_question_label_confidence
            else None
        )
        anchor = {
            "anchor_id": f"anchor-{index}",
            "text": text,
            "confidence": round(confidence, 6),
            "bbox": list(bbox) if bbox is not None else None,
            "question_id": question_id,
        }
        audit_anchors.append(anchor)
        if question_id is not None and bbox is not None:
            resolved.append(anchor)
    resolved.sort(key=lambda item: (item["bbox"][1], item["bbox"][0], item["anchor_id"]))
    deduplicated: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for anchor in resolved:
        question_id = str(anchor["question_id"])
        if question_id in positions:
            current = deduplicated[positions[question_id]]
            if abs(int(anchor["bbox"][1]) - int(current["bbox"][1])) > max(8, round(height * 0.03)):
                reasons.append("ambiguous_question_label")
            if float(anchor["confidence"]) > float(current["confidence"]):
                deduplicated[positions[question_id]] = anchor
            continue
        positions[question_id] = len(deduplicated)
        deduplicated.append(anchor)
    deduplicated.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    if not deduplicated:
        reasons.append("no_resolved_question_ids")
    return deduplicated, audit_anchors, reasons


def _geometry_groups(
    content: list[dict[str, Any]],
    *,
    height: int,
    settings: LocalLayoutSettings,
) -> tuple[list[list[dict[str, Any]]], float]:
    heights = [region["bbox"][3] - region["bbox"][1] for region in content]
    median_height = statistics.median(heights)
    gap_threshold = min(
        max(
            settings.merge_gap_height_multiplier * median_height,
            settings.merge_gap_min_page_ratio * height,
        ),
        settings.merge_gap_max_page_ratio * height,
    )
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bottom = -1
    for region in content:
        y1, y2 = region["bbox"][1], region["bbox"][3]
        gap = y1 - current_bottom if current else 0
        starts_title = region["region_type"] == "paragraph_title"
        if current and (starts_title or gap > gap_threshold):
            groups.append(current)
            current = []
        current.append(region)
        current_bottom = max(current_bottom, y2) if len(current) > 1 else y2
    if current:
        groups.append(current)
    return groups, float(gap_threshold)


def _merge_default_regions(
    content: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    page: int,
    settings: LocalLayoutSettings,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    if not content:
        return [], ["no_question_regions"], {"content_count": 0, "anchor_count": len(anchors)}
    groups, gap_threshold = _geometry_groups(content, height=height, settings=settings)
    horizontal_pad = round(width * settings.merge_horizontal_padding_ratio)
    vertical_pad = round(height * settings.merge_vertical_padding_ratio)
    global_x1 = max(0, min(region["bbox"][0] for region in content) - horizontal_pad)
    global_x2 = min(width, max(region["bbox"][2] for region in content) + horizontal_pad)
    debug = {
        "content_count": len(content),
        "anchor_count": len(anchors),
        "geometry_group_count": len(groups),
        "gap_threshold": round(gap_threshold, 3),
    }
    if not anchors:
        return [], ["no_resolved_question_ids"], debug

    merged: list[dict[str, Any]] = []
    reasons: list[str] = []
    content_top = min(region["bbox"][1] for region in content)
    first_anchor_top = int(anchors[0]["bbox"][1])
    has_unresolved_prefix = content_top < first_anchor_top - height * settings.merge_gap_max_page_ratio
    if has_unresolved_prefix:
        reasons.append("unresolved_content_region")

    for anchor_index, anchor in enumerate(anchors):
        anchor_top = int(anchor["bbox"][1])
        if anchor_index == 0 and not has_unresolved_prefix:
            top = max(0, min(content_top, anchor_top) - vertical_pad)
        else:
            top = max(0, anchor_top - round(height * 0.012))
        if anchor_index + 1 < len(anchors):
            bottom = max(top + 1, int(anchors[anchor_index + 1]["bbox"][1]) - round(height * 0.012))
        else:
            bottom = min(height, max(region["bbox"][3] for region in content) + vertical_pad)
        assigned = [
            region
            for region in content
            if top <= (region["bbox"][1] + region["bbox"][3]) / 2 < bottom
        ]
        region_score = (
            statistics.fmean(float(region["score"]) for region in assigned)
            if assigned
            else float(anchor["confidence"])
        )
        merged.append(
            {
                "region_id": f"local-p{page}-merged-{anchor_index + 1}",
                "region_type": "question_block",
                "bbox": [global_x1, top, global_x2, bottom],
                "score": round(region_score, 6),
                "reading_order": anchor_index,
                "question_label": str(anchor["text"]),
                "question_label_confidence": round(float(anchor["confidence"]), 6),
                "question_id": str(anchor["question_id"]),
                "source_region_ids": [str(region["region_id"]) for region in assigned],
            }
        )
    return merged, reasons, debug


class LocalLayoutObserver:
    """Answer-blind local layout observer with a conservative acceptance gate."""

    def __init__(
        self,
        settings: LocalLayoutSettings,
        manifest: AnswerManifest,
        *,
        backend: LocalLayoutBackend | None = None,
        label_reader: QuestionLabelReader | None = None,
    ) -> None:
        self.settings = settings
        self.manifest = manifest
        self.backend = backend or PaddleLayoutBackend(settings)
        self.label_reader = label_reader or (
            PaddleOCRQuestionLabelReader(settings) if settings.question_id_ocr_enabled else None
        )

    def _observe_default_model(
        self,
        image_path: Path,
        *,
        page: int,
        width: int,
        height: int,
        raw_boxes: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        content, reasons = _default_content_regions(
            raw_boxes,
            width=width,
            height=height,
            page=page,
            settings=self.settings,
        )
        if not content:
            reasons = sorted(set([*reasons, "no_question_regions"]))
            return {
                "accepted": False,
                "observation": {
                    "page": page,
                    "page_type": "unknown",
                    "rotation_degrees_clockwise": 0,
                    "orientation_confidence": 0.0,
                    "questions": [],
                },
                "audit": {
                    "source": "local_layout",
                    "mode": "default_model_rule_merge",
                    "status": "rejected",
                    "page": page,
                    "model_name": self.settings.model_name,
                    "engine": self.settings.engine,
                    "reasons": reasons,
                    "question_anchors": [],
                    "raw_regions": [],
                    "regions": [],
                    "merge": {"content_count": 0, "anchor_count": 0},
                },
            }
        anchors, audit_anchors, anchor_reasons = _resolved_page_anchors(
            self.label_reader,
            image_path,
            self.manifest,
            self.settings,
            width=width,
            height=height,
        )
        merged, merge_reasons, merge_debug = _merge_default_regions(
            content,
            anchors,
            width=width,
            height=height,
            page=page,
            settings=self.settings,
        )
        reasons.extend(anchor_reasons)
        reasons.extend(merge_reasons)
        resolved_regions = [region for region in merged if region.get("question_id")]
        for left_index, left in enumerate(resolved_regions):
            for right in resolved_regions[left_index + 1 :]:
                if left["question_id"] == right["question_id"]:
                    reasons.append("ambiguous_question_label")
                    continue
                if (
                    _intersection_over_smaller(tuple(left["bbox"]), tuple(right["bbox"]))
                    > self.settings.max_sibling_overlap_ratio
                ):
                    reasons.append("overlapping_sibling_questions")
        reasons = sorted(set(reasons))
        blocking_reasons = {
            "ambiguous_question_label",
            "invalid_content_bbox",
            "no_question_regions",
            "no_resolved_question_ids",
            "overlapping_sibling_questions",
            "question_id_ocr_unavailable",
            "unresolved_content_region",
        }
        accepted = bool(resolved_regions) and not any(reason in blocking_reasons for reason in reasons)
        questions = [
            {
                "question_id": str(region["question_id"]),
                "bbox": list(region["bbox"]),
                "confidence": min(
                    float(region["score"]),
                    float(region["question_label_confidence"]),
                ),
                "question_type": "question_block",
                "artifact_ref": str(image_path),
            }
            for region in resolved_regions
        ]
        return {
            "accepted": accepted,
            "observation": {
                "page": page,
                "page_type": "assignment" if questions else "unknown",
                "rotation_degrees_clockwise": 0,
                "orientation_confidence": 0.0,
                "questions": questions,
            },
            "audit": {
                "source": "local_layout",
                "mode": "default_model_rule_merge",
                "status": "accepted" if accepted else "rejected",
                "page": page,
                "model_name": self.settings.model_name,
                "engine": self.settings.engine,
                "reasons": reasons,
                "question_anchors": audit_anchors,
                "raw_regions": content,
                "regions": merged,
                "merge": merge_debug,
            },
        }

    def observe(self, image_path: Path | str, *, page: int) -> dict[str, Any]:
        if not self.settings.enabled:
            raise LocalLayoutUnavailable("local layout is disabled")
        image_path = Path(image_path).resolve()
        with Image.open(image_path) as image:
            width, height = image.size
        raw_outputs = list(self.backend.predict(image_path))
        raw_boxes: list[Mapping[str, Any]] = []
        for output in raw_outputs:
            raw_boxes.extend(_result_boxes(output))
        mapped_labels = {
            self.settings.label_map.get(
                str(item.get("label") or item.get("region_type") or item.get("class_name") or "unknown"),
                str(item.get("label") or item.get("region_type") or item.get("class_name") or "unknown"),
            )
            for item in raw_boxes
        }
        has_project_content = bool(
            mapped_labels & {"question_block", "subquestion", "student_answer", "cross_page_continuation"}
        )
        if self.settings.merge_default_regions and not has_project_content:
            return self._observe_default_model(
                image_path,
                page=page,
                width=width,
                height=height,
                raw_boxes=raw_boxes,
            )
        regions: list[dict[str, Any]] = []
        reasons: list[str] = []
        for index, item in enumerate(raw_boxes, 1):
            raw_label = str(item.get("label") or item.get("region_type") or item.get("class_name") or "unknown")
            region_type = self.settings.label_map.get(raw_label, raw_label)
            if region_type not in SUPPORTED_REGION_TYPES:
                continue
            try:
                score = float(item.get("score", item.get("confidence", 0.0)) or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            bbox = _bbox_from_item(item, width, height)
            if bbox is None:
                if region_type in CONTENT_REGION_TYPES:
                    reasons.append("invalid_content_bbox")
                continue
            area_ratio = ((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / max(1, width * height)
            if score < self.settings.min_region_confidence:
                if region_type in CONTENT_REGION_TYPES:
                    reasons.append("low_content_region_confidence")
                continue
            if area_ratio < self.settings.min_region_area_ratio:
                if region_type in CONTENT_REGION_TYPES:
                    reasons.append("content_region_too_small")
                continue
            question_label = str(item.get("question_label") or item.get("text") or "").strip()
            label_confidence = float(item.get("question_label_confidence", score) or 0.0) if question_label else 0.0
            if not question_label and region_type in {"question_block", "subquestion"} and self.label_reader is not None:
                try:
                    candidates = self.label_reader.read(image_path, bbox)
                except LocalLayoutUnavailable:
                    candidates = []
                    reasons.append("question_id_ocr_unavailable")
                unique: dict[str, float] = {}
                for text, confidence in candidates:
                    resolved = resolve_question_label(text, self.manifest)
                    if resolved:
                        unique[resolved] = max(unique.get(resolved, 0.0), float(confidence))
                if len(unique) == 1:
                    question_label, label_confidence = next(iter(unique.items()))
                elif len(unique) > 1:
                    reasons.append("ambiguous_question_label")
            resolved_question_id = (
                resolve_question_label(question_label, self.manifest)
                if question_label and label_confidence >= self.settings.min_question_label_confidence
                else None
            )
            regions.append(
                {
                    "region_id": str(item.get("region_id") or f"local-p{page}-r{index}"),
                    "region_type": region_type,
                    "bbox": list(bbox),
                    "score": round(score, 6),
                    "reading_order": int(item.get("reading_order", item.get("order", index - 1)) or 0),
                    "question_label": question_label,
                    "question_label_confidence": round(label_confidence, 6),
                    "question_id": resolved_question_id,
                }
            )

        question_regions = [
            region for region in regions if region["region_type"] in {"question_block", "subquestion"}
        ]
        unresolved_content = [
            region
            for region in regions
            if region["region_type"] in CONTENT_REGION_TYPES and not region.get("question_id")
        ]
        if unresolved_content:
            reasons.append("unresolved_content_region")
        if not question_regions:
            reasons.append("no_question_regions")
        if question_regions and not any(region.get("question_id") for region in question_regions):
            reasons.append("no_resolved_question_ids")
        resolved_regions = [region for region in question_regions if region.get("question_id")]
        for left_index, left in enumerate(resolved_regions):
            for right in resolved_regions[left_index + 1 :]:
                if left["question_id"] == right["question_id"]:
                    continue
                if {left["region_type"], right["region_type"]} == {"question_block", "subquestion"}:
                    continue
                if _intersection_over_smaller(tuple(left["bbox"]), tuple(right["bbox"])) > self.settings.max_sibling_overlap_ratio:
                    reasons.append("overlapping_sibling_questions")

        reasons = sorted(set(reasons))
        blocking_reasons = {
            "ambiguous_question_label",
            "unresolved_content_region",
            "no_question_regions",
            "no_resolved_question_ids",
            "overlapping_sibling_questions",
            "invalid_content_bbox",
            "low_content_region_confidence",
            "content_region_too_small",
        }
        accepted = not any(reason in blocking_reasons for reason in reasons)
        questions = [
            {
                "question_id": str(region["question_id"]),
                "bbox": list(region["bbox"]),
                "confidence": min(float(region["score"]), float(region["question_label_confidence"])),
                "question_type": region["region_type"],
                "artifact_ref": str(image_path),
            }
            for region in sorted(resolved_regions, key=lambda item: (item["reading_order"], item["region_id"]))
        ]
        return {
            "accepted": accepted,
            "observation": {
                "page": page,
                "page_type": "assignment" if questions else "unknown",
                "rotation_degrees_clockwise": 0,
                "orientation_confidence": 0.0,
                "questions": questions,
            },
            "audit": {
                "source": "local_layout",
                "status": "accepted" if accepted else "rejected",
                "page": page,
                "model_name": self.settings.model_name,
                "engine": self.settings.engine,
                "reasons": reasons,
                "regions": regions,
            },
        }


__all__ = [
    "CONTENT_REGION_TYPES",
    "LocalLayoutBackend",
    "LocalLayoutObserver",
    "LocalLayoutSettings",
    "LocalLayoutUnavailable",
    "PaddleLayoutBackend",
    "PaddleOCRQuestionLabelReader",
    "QuestionLabelReader",
    "normalize_question_label",
    "question_label_candidates",
    "resolve_question_label",
]
