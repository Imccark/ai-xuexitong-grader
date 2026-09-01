from __future__ import annotations

from functools import lru_cache
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


MODEL_NAME = "PP-LCNet_x1_0_doc_ori"
MODEL_SHA256 = "af9a0a4f317ff0709ce752067807f819cb15d883f8ecad89f28df1c6ee2d9c92"
DEFAULT_MODEL_PATH = (
    Path(__file__).resolve().parents[2]
    / "models"
    / "document_orientation"
    / "PP-LCNet_x1_0_doc_ori.onnx"
)
ORIENTATION_LABELS = (0, 90, 180, 270)
AUTO_ROTATE_MIN_CONFIDENCE = 0.85
AUTO_ROTATE_MIN_MARGIN = 0.15


def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - np.max(values, axis=-1, keepdims=True)
    exponentials = np.exp(shifted)
    return exponentials / np.sum(exponentials, axis=-1, keepdims=True)


def _resize_short_and_center_crop(image: Image.Image) -> Image.Image:
    image = image.convert("RGB")
    width, height = image.size
    scale = 256.0 / max(1, min(width, height))
    resized = image.resize(
        (max(224, int(round(width * scale))), max(224, int(round(height * scale)))),
        Image.Resampling.BILINEAR,
    )
    left = max(0, (resized.width - 224) // 2)
    top = max(0, (resized.height - 224) // 2)
    return resized.crop((left, top, left + 224, top + 224))


def _input_tensor(image: Image.Image) -> np.ndarray:
    array = np.asarray(_resize_short_and_center_crop(image), dtype=np.float32) / 255.0
    mean = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
    array = (array - mean) / std
    return np.transpose(array, (2, 0, 1))[None, ...].astype(np.float32)


@lru_cache(maxsize=2)
def _session(model_path: str):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 3
    options.intra_op_num_threads = 1
    return ort.InferenceSession(model_path, sess_options=options, providers=["CPUExecutionProvider"])


@lru_cache(maxsize=2)
def _model_hash(model_path: str) -> str:
    return hashlib.sha256(Path(model_path).read_bytes()).hexdigest()


def classify_document_orientation(
    image: Image.Image,
    *,
    model_path: Path | str = DEFAULT_MODEL_PATH,
) -> dict[str, Any]:
    """Classify page orientation and return the inverse correction rotation."""

    path = Path(model_path).resolve()
    if not path.is_file():
        return {
            "available": False,
            "applied": False,
            "reason": "orientation_model_missing",
            "rotation_degrees_clockwise": 0,
            "predicted_orientation_degrees_clockwise": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "orientation_scores": {},
            "model": MODEL_NAME,
            "model_sha256": MODEL_SHA256,
        }
    if _model_hash(str(path)) != MODEL_SHA256:
        return {
            "available": False,
            "applied": False,
            "reason": "orientation_model_hash_mismatch",
            "rotation_degrees_clockwise": 0,
            "predicted_orientation_degrees_clockwise": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "orientation_scores": {},
            "model": MODEL_NAME,
            "model_sha256": MODEL_SHA256,
        }

    try:
        session = _session(str(path))
    except Exception as exc:
        return {
            "available": False,
            "applied": False,
            "reason": f"orientation_runtime_unavailable:{type(exc).__name__}",
            "rotation_degrees_clockwise": 0,
            "predicted_orientation_degrees_clockwise": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "orientation_scores": {},
            "model": MODEL_NAME,
            "model_sha256": MODEL_SHA256,
        }
    raw = np.asarray(
        session.run(None, {session.get_inputs()[0].name: _input_tensor(image)})[0],
        dtype=np.float32,
    ).reshape(-1)
    probabilities = raw if np.all(raw >= 0) and abs(float(raw.sum()) - 1.0) < 1e-3 else _softmax(raw)
    order = np.argsort(probabilities)[::-1]
    best_index = int(order[0])
    confidence = float(probabilities[best_index])
    margin = confidence - float(probabilities[int(order[1])])
    predicted_orientation = ORIENTATION_LABELS[best_index]
    correction_rotation = (-predicted_orientation) % 360
    accepted = confidence >= AUTO_ROTATE_MIN_CONFIDENCE and margin >= AUTO_ROTATE_MIN_MARGIN
    return {
        "available": True,
        "applied": bool(accepted and correction_rotation != 0),
        "reason": "accepted" if accepted else "low_orientation_confidence",
        "rotation_degrees_clockwise": int(correction_rotation if accepted else 0),
        "predicted_orientation_degrees_clockwise": int(predicted_orientation),
        "confidence": round(confidence, 6),
        "margin": round(margin, 6),
        "orientation_scores": {
            str(label): round(float(probabilities[index]), 6)
            for index, label in enumerate(ORIENTATION_LABELS)
        },
        "model": MODEL_NAME,
        "model_sha256": MODEL_SHA256,
    }


def rotate_clockwise(image: Image.Image, degrees: int) -> Image.Image:
    operation = {
        0: None,
        90: Image.Transpose.ROTATE_270,
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_90,
    }.get(int(degrees))
    if int(degrees) not in ORIENTATION_LABELS:
        raise ValueError(f"unsupported document rotation: {degrees}")
    return image.copy() if operation is None else image.transpose(operation)


def clockwise_rotation_matrix(width: int, height: int, degrees: int) -> tuple[np.ndarray, tuple[int, int]]:
    degrees = int(degrees)
    if degrees == 0:
        return np.eye(3, dtype=np.float64), (width, height)
    if degrees == 90:
        return np.asarray([[0, -1, height - 1], [1, 0, 0], [0, 0, 1]], dtype=np.float64), (height, width)
    if degrees == 180:
        return np.asarray([[-1, 0, width - 1], [0, -1, height - 1], [0, 0, 1]], dtype=np.float64), (width, height)
    if degrees == 270:
        return np.asarray([[0, 1, 0], [-1, 0, width - 1], [0, 0, 1]], dtype=np.float64), (height, width)
    raise ValueError(f"unsupported document rotation: {degrees}")
