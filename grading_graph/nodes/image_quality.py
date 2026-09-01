from __future__ import annotations

import io
import json
import warnings
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageEnhance, ImageOps

from grading_graph.nodes.orientation import (
    classify_document_orientation,
    clockwise_rotation_matrix,
    rotate_clockwise,
)
from grading_graph.store import atomic_write_bytes, atomic_write_json


MAX_DEFAULT_PIXELS = 120_000_000
WORKING_MAX_SIDE = 3200
WORKING_MAX_PIXELS = 12_000_000
RECTIFICATION_VERSION = "document-rectification-v4-conservative-candidate-search"
MIN_DOCUMENT_AREA_RATIO = 0.20
MIN_DOCUMENT_CONFIDENCE = 0.68
AMBIGUOUS_ORIENTATION_MIN_UPRIGHT_CONFIDENCE = 0.90
AMBIGUOUS_ORIENTATION_MIN_CANDIDATE_MARGIN = 0.75


def _resolve_ambiguous_orientation(image: Image.Image, initial: dict[str, Any]) -> dict[str, Any]:
    """Use cheap four-direction verification when the first orientation vote is uncertain."""
    if initial.get("reason") != "low_orientation_confidence" or not initial.get("available"):
        return initial
    candidates: list[dict[str, Any]] = []
    for degrees in (0, 90, 180, 270):
        result = initial if degrees == 0 else classify_document_orientation(rotate_clockwise(image, degrees))
        upright_score = float((result.get("orientation_scores") or {}).get("0", 0.0) or 0.0)
        candidates.append(
            {
                "rotation_degrees_clockwise": degrees,
                "upright_score": upright_score,
                "result": result,
            }
        )
    ranked = sorted(candidates, key=lambda item: (-item["upright_score"], item["rotation_degrees_clockwise"]))
    best, runner_up = ranked[:2]
    candidate_margin = best["upright_score"] - runner_up["upright_score"]
    best_result = best["result"]
    accepted = bool(
        best["upright_score"] >= AMBIGUOUS_ORIENTATION_MIN_UPRIGHT_CONFIDENCE
        and candidate_margin >= AMBIGUOUS_ORIENTATION_MIN_CANDIDATE_MARGIN
        and int(best_result.get("predicted_orientation_degrees_clockwise", -1)) == 0
        and best_result.get("reason") == "accepted"
    )
    search = {
        "accepted": accepted,
        "selected_rotation_degrees_clockwise": int(best["rotation_degrees_clockwise"]),
        "selected_upright_confidence": round(float(best["upright_score"]), 6),
        "candidate_margin": round(float(candidate_margin), 6),
        "upright_scores": {
            str(item["rotation_degrees_clockwise"]): round(float(item["upright_score"]), 6)
            for item in candidates
        },
    }
    if not accepted:
        return {**initial, "candidate_search": search}
    selected_rotation = int(best["rotation_degrees_clockwise"])
    return {
        **initial,
        "applied": selected_rotation != 0,
        "reason": "accepted_by_upright_candidate_search",
        "rotation_degrees_clockwise": selected_rotation,
        "confidence": round(float(best["upright_score"]), 6),
        "margin": round(float(candidate_margin), 6),
        "candidate_search": search,
    }


def _oriented_size(size: tuple[int, int], orientation: int | None) -> tuple[int, int]:
    if orientation in {5, 6, 7, 8}:
        return size[1], size[0]
    return size


def _apply_exif_orientation(image: Image.Image, orientation: int | None) -> Image.Image:
    operations = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }
    operation = operations.get(orientation)
    return image.transpose(operation) if operation is not None else image


def _decode_reduced(
    data: bytes,
    *,
    source_size: tuple[int, int],
    orientation: int | None,
    max_side: int,
) -> Image.Image:
    """Decode an oversized raster at a bounded working resolution.

    Pillow's PNG decoder can materialize a 113MP image before ``thumbnail``
    gets a chance to reduce it. OpenCV's reduced decode path avoids that peak
    for the image-quality/normalization working copy; the original bytes are
    still retained separately by the caller.
    """
    try:
        import cv2

        import numpy as np
    except ImportError as exc:  # pragma: no cover - dependency is locked in pyproject
        raise ValueError("reduced image decoder is unavailable") from exc

    longest_side = max(source_size)
    reduction = 1
    while reduction < 8 and longest_side / reduction > max_side:
        reduction *= 2
    flags = {
        1: cv2.IMREAD_COLOR,
        2: cv2.IMREAD_REDUCED_COLOR_2,
        4: cv2.IMREAD_REDUCED_COLOR_4,
        8: cv2.IMREAD_REDUCED_COLOR_8,
    }[reduction]
    array = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), flags)
    if array is None or array.size == 0:
        raise ValueError("reduced image decode failed")
    if array.ndim == 2:
        image = Image.fromarray(array, mode="L").convert("RGB")
    else:
        image = Image.fromarray(cv2.cvtColor(array, cv2.COLOR_BGR2RGB), mode="RGB")
    image = _apply_exif_orientation(image, orientation)
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    image.load()
    return image


def _open_image(
    data: bytes,
    *,
    max_pixels: int = MAX_DEFAULT_PIXELS,
    working_max_side: int | None = None,
) -> tuple[Image.Image, int | None, tuple[int, int]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        header = Image.open(io.BytesIO(data))
        width, height = header.size
        if width * height > max_pixels:
            raise ValueError(f"image exceeds pixel limit: {width * height} > {max_pixels}")
        exif_orientation = header.getexif().get(274)
        oriented_size = _oriented_size((width, height), exif_orientation)
        if working_max_side and max(oriented_size) > working_max_side:
            header.close()
            return (
                _decode_reduced(
                    data,
                    source_size=(width, height),
                    orientation=exif_orientation,
                    max_side=working_max_side,
                ),
                exif_orientation,
                oriented_size,
            )
        header.close()
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)
        image.load()
    if image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    elif image.mode == "L":
        image = image.convert("RGB")
    if working_max_side:
        image.thumbnail((working_max_side, working_max_side), Image.Resampling.LANCZOS)
    return image, exif_orientation, oriented_size


def _encode_png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def normalize_image_bytes(
    data: bytes,
    *,
    max_side: int = WORKING_MAX_SIDE,
    max_pixels: int = MAX_DEFAULT_PIXELS,
) -> tuple[bytes, dict[str, Any]]:
    image, exif_orientation, original_size = _open_image(
        data,
        max_pixels=max_pixels,
        working_max_side=max_side,
    )
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    metadata = {
        "original_width": original_size[0],
        "original_height": original_size[1],
        "normalized_width": image.width,
        "normalized_height": image.height,
        "exif_orientation": exif_orientation,
        "orientation_applied": exif_orientation not in {None, 1},
        "working_copy_max_side": max_side,
    }
    return _encode_png(image), metadata


def _order_quad(points: np.ndarray) -> np.ndarray:
    """Return document corners as top-left, top-right, bottom-right, bottom-left."""
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    ordered = np.zeros((4, 2), dtype=np.float32)
    sums = points.sum(axis=1)
    differences = np.diff(points, axis=1).reshape(-1)
    ordered[0] = points[int(np.argmin(sums))]
    ordered[2] = points[int(np.argmax(sums))]
    ordered[1] = points[int(np.argmin(differences))]
    ordered[3] = points[int(np.argmax(differences))]
    return ordered


def _quad_angle_score(quad: np.ndarray) -> float:
    ordered = _order_quad(quad)
    cosine_errors: list[float] = []
    for index in range(4):
        previous = ordered[(index - 1) % 4] - ordered[index]
        following = ordered[(index + 1) % 4] - ordered[index]
        denominator = float(np.linalg.norm(previous) * np.linalg.norm(following))
        cosine_errors.append(abs(float(np.dot(previous, following)) / denominator) if denominator else 1.0)
    return max(0.0, 1.0 - float(np.mean(cosine_errors)))


def _document_candidate_masks(gray: np.ndarray) -> list[tuple[str, np.ndarray]]:
    import cv2

    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    median = float(np.median(blurred))
    lower = int(max(20, 0.55 * median))
    upper = int(min(255, max(lower + 30, 1.35 * median)))
    edges = cv2.Canny(blurred, lower, upper)
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, close_kernel, iterations=2)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=1)

    _threshold, bright = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, close_kernel, iterations=2)
    bright = cv2.morphologyEx(bright, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8), iterations=1)
    return [("edge_contour", edges), ("bright_paper", bright)]


def _detect_document_quad(rgb: np.ndarray) -> tuple[np.ndarray | None, dict[str, Any]]:
    import cv2

    height, width = rgb.shape[:2]
    image_area = float(width * height)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    candidates: list[dict[str, Any]] = []
    for method, mask in _document_candidate_masks(gray):
        contours, _hierarchy = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:24]:
            contour_area = float(cv2.contourArea(contour))
            area_ratio = contour_area / image_area if image_area else 0.0
            if area_ratio < MIN_DOCUMENT_AREA_RATIO or area_ratio > 0.985:
                continue
            perimeter = float(cv2.arcLength(contour, True))
            if perimeter <= 0:
                continue
            quads: list[np.ndarray] = []
            for epsilon_ratio in (0.015, 0.02, 0.03, 0.04, 0.055):
                approximation = cv2.approxPolyDP(contour, epsilon_ratio * perimeter, True)
                if len(approximation) == 4 and cv2.isContourConvex(approximation):
                    quads.append(approximation.reshape(4, 2).astype(np.float32))
                    break
            if not quads:
                rectangle = cv2.minAreaRect(contour)
                box = cv2.boxPoints(rectangle).astype(np.float32)
                rectangle_area = max(1.0, float(rectangle[1][0] * rectangle[1][1]))
                if contour_area / rectangle_area >= 0.88:
                    quads.append(box)
            for quad in quads:
                ordered = _order_quad(quad)
                polygon_area = abs(float(cv2.contourArea(ordered)))
                polygon_ratio = polygon_area / image_area if image_area else 0.0
                rectangle = cv2.minAreaRect(ordered)
                rectangle_area = max(1.0, float(rectangle[1][0] * rectangle[1][1]))
                rectangularity = min(1.0, polygon_area / rectangle_area)
                angle_score = _quad_angle_score(ordered)
                side_lengths = [
                    float(np.linalg.norm(ordered[(index + 1) % 4] - ordered[index]))
                    for index in range(4)
                ]
                side_balance = min(side_lengths) / max(side_lengths) if max(side_lengths) else 0.0
                balance_score = min(1.0, side_balance / 0.42)
                # Large pale desks and tabletops are common around homework
                # pages.  Size alone must not outrank a slightly smaller paper
                # contour that has a bright, moderately uniform surface plus a
                # plausible amount of dark handwriting.
                polygon_mask = np.zeros_like(gray, dtype=np.uint8)
                cv2.fillConvexPoly(polygon_mask, np.rint(ordered).astype(np.int32), 255)
                pixels = gray[polygon_mask > 0]
                mean_brightness = float(pixels.mean()) if pixels.size else 0.0
                brightness_std = float(pixels.std()) if pixels.size else 255.0
                dark_ratio = float(np.mean(pixels < 150)) if pixels.size else 1.0
                brightness_score = max(0.0, min(1.0, (mean_brightness - 105.0) / 120.0))
                uniformity_score = max(0.0, min(1.0, 1.0 - brightness_std / 105.0))
                if 0.004 <= dark_ratio <= 0.20:
                    ink_score = 1.0
                elif dark_ratio < 0.004:
                    ink_score = dark_ratio / 0.004
                else:
                    ink_score = max(0.0, 1.0 - (dark_ratio - 0.20) / 0.35)
                paper_score = 0.48 * brightness_score + 0.27 * uniformity_score + 0.25 * ink_score
                area_score = min(1.0, polygon_ratio / 0.68)
                method_bonus = 0.05 if method == "edge_contour" else 0.0
                oversized_bright_penalty = 0.14 if method == "bright_paper" and polygon_ratio > 0.82 else 0.0
                confidence = (
                    0.24 * area_score
                    + 0.21 * rectangularity
                    + 0.19 * angle_score
                    + 0.08 * balance_score
                    + 0.28 * paper_score
                    + method_bonus
                    - oversized_bright_penalty
                )
                candidates.append(
                    {
                        "quad": ordered,
                        "method": method,
                        "confidence": confidence,
                        "area_ratio": polygon_ratio,
                        "rectangularity": rectangularity,
                        "angle_score": angle_score,
                        "mean_brightness": mean_brightness,
                        "brightness_std": brightness_std,
                        "dark_ratio": dark_ratio,
                        "paper_score": paper_score,
                    }
                )
    if not candidates:
        return None, {"detected": False, "applied": False, "reason": "no_document_quadrilateral"}
    best = max(candidates, key=lambda item: (item["confidence"], item["area_ratio"]))
    hard_gate_passed = bool(
        best["confidence"] >= MIN_DOCUMENT_CONFIDENCE
        and best["paper_score"] >= 0.58
        and best["dark_ratio"] <= 0.135
        and best["angle_score"] >= 0.72
    )
    rejection_reason = "accepted"
    if not hard_gate_passed:
        if best["dark_ratio"] > 0.135:
            rejection_reason = "candidate_contains_too_much_nonpaper_content"
        elif best["paper_score"] < 0.58:
            rejection_reason = "candidate_not_paper_like"
        elif best["angle_score"] < 0.72:
            rejection_reason = "candidate_geometry_not_rectangular"
        else:
            rejection_reason = "low_document_confidence"
    metadata = {
        "detected": True,
        "applied": hard_gate_passed,
        "method": best["method"],
        "confidence": round(float(best["confidence"]), 6),
        "area_ratio": round(float(best["area_ratio"]), 6),
        "rectangularity": round(float(best["rectangularity"]), 6),
        "angle_score": round(float(best["angle_score"]), 6),
        "mean_brightness": round(float(best["mean_brightness"]), 4),
        "brightness_std": round(float(best["brightness_std"]), 4),
        "dark_ratio": round(float(best["dark_ratio"]), 6),
        "paper_score": round(float(best["paper_score"]), 6),
        "reason": rejection_reason,
        "candidate_summary": [
            {
                "method": item["method"],
                "confidence": round(float(item["confidence"]), 6),
                "area_ratio": round(float(item["area_ratio"]), 6),
                "paper_score": round(float(item["paper_score"]), 6),
                "dark_ratio": round(float(item["dark_ratio"]), 6),
            }
            for item in sorted(candidates, key=lambda item: item["confidence"], reverse=True)[:5]
        ],
    }
    return (best["quad"] if hard_gate_passed else None), metadata


def _warp_document(rgb: np.ndarray, quad: np.ndarray, *, max_side: int) -> tuple[np.ndarray, np.ndarray]:
    import cv2

    top_left, top_right, bottom_right, bottom_left = _order_quad(quad)
    width = max(float(np.linalg.norm(top_right - top_left)), float(np.linalg.norm(bottom_right - bottom_left)))
    height = max(float(np.linalg.norm(bottom_left - top_left)), float(np.linalg.norm(bottom_right - top_right)))
    target_width = max(2, int(round(width)))
    target_height = max(2, int(round(height)))
    scale = min(1.0, max_side / max(target_width, target_height))
    target_width = max(2, int(round(target_width * scale)))
    target_height = max(2, int(round(target_height * scale)))
    destination = np.asarray(
        [[0, 0], [target_width - 1, 0], [target_width - 1, target_height - 1], [0, target_height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(_order_quad(quad), destination)
    warped = cv2.warpPerspective(
        rgb,
        matrix,
        (target_width, target_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return warped, matrix


def rectify_document_bytes(
    data: bytes,
    *,
    max_side: int = WORKING_MAX_SIDE,
    max_pixels: int = MAX_DEFAULT_PIXELS,
    orientation_override_degrees: int = 0,
) -> tuple[bytes, dict[str, Any]]:
    """Detect a photographed paper sheet and create an auditable flat view.

    A low-confidence detection is never warped: callers receive the existing
    EXIF-normalized working copy plus an explicit fallback reason.
    """
    if orientation_override_degrees not in (0, 90, 180, 270):
        raise ValueError("orientation_override_degrees must be one of 0, 90, 180, 270")
    image, exif_orientation, original_size = _open_image(
        data,
        max_pixels=max_pixels,
        working_max_side=max_side,
    )
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    working_height, working_width = rgb.shape[:2]
    quad, detection = _detect_document_quad(rgb)
    metadata: dict[str, Any] = {
        "version": RECTIFICATION_VERSION,
        "original_width": original_size[0],
        "original_height": original_size[1],
        "working_width": working_width,
        "working_height": working_height,
        "exif_orientation": exif_orientation,
        "exif_orientation_applied": exif_orientation not in {None, 1},
        **detection,
    }
    scale_to_working = np.asarray(
        [
            [working_width / max(1, original_size[0]), 0, 0],
            [0, working_height / max(1, original_size[1]), 0],
            [0, 0, 1],
        ],
        dtype=np.float64,
    )
    if quad is None:
        flat = image.convert("RGB")
        base_matrix = scale_to_working
        normalized_quad: list[list[float]] = []
        fallback_used = True
    else:
        warped, working_matrix = _warp_document(rgb, quad, max_side=max_side)
        flat = Image.fromarray(warped, mode="RGB")
        base_matrix = np.asarray(working_matrix, dtype=np.float64) @ scale_to_working
        normalized_quad = [
            [round(float(x) / max(1, working_width), 8), round(float(y) / max(1, working_height), 8)]
            for x, y in _order_quad(quad)
        ]
        fallback_used = False

    gray = np.asarray(flat.convert("L"), dtype=np.uint8)
    if float(np.mean(gray < 220)) < 0.005:
        orientation = {
            "available": True,
            "applied": False,
            "reason": "near_blank_orientation_skipped",
            "rotation_degrees_clockwise": 0,
            "predicted_orientation_degrees_clockwise": 0,
            "confidence": 1.0,
            "margin": 1.0,
            "orientation_scores": {"0": 1.0, "90": 0.0, "180": 0.0, "270": 0.0},
            "model": "blank-page-gate",
            "model_sha256": "",
        }
    else:
        orientation = classify_document_orientation(flat)
        orientation = _resolve_ambiguous_orientation(flat, orientation)
    rotation_degrees = int(orientation.get("rotation_degrees_clockwise", 0) or 0)
    upright = rotate_clockwise(flat, rotation_degrees)
    if rotation_degrees:
        verification = classify_document_orientation(upright)
        orientation = {**orientation, "verification": verification}
        residual_rotation = int(verification.get("rotation_degrees_clockwise", 0) or 0)
        if verification.get("reason") == "accepted" and residual_rotation:
            # A trustworthy classifier should report upright after its own
            # correction. Revert instead of oscillating on contradictory pages;
            # PageObserver can resolve these rare cases with multimodal context.
            orientation.update(
                {
                    "applied": False,
                    "reason": "unstable_orientation_cycle",
                    "rotation_degrees_clockwise": 0,
                }
            )
            rotation_degrees = 0
            upright = flat.copy()
    if orientation_override_degrees:
        pre_override_rotation = rotation_degrees
        upright = rotate_clockwise(upright, orientation_override_degrees)
        rotation_degrees = (rotation_degrees + orientation_override_degrees) % 360
        override_verification = classify_document_orientation(upright)
        residual_rotation = int(override_verification.get("rotation_degrees_clockwise", 0) or 0)
        if override_verification.get("reason") == "accepted" and residual_rotation:
            raise RuntimeError("teacher orientation override failed local residual verification")
        orientation = {
            **orientation,
            "applied": rotation_degrees != 0,
            "reason": "teacher_rotation_override",
            "rotation_degrees_clockwise": rotation_degrees,
            "teacher_override": {
                "additional_rotation_degrees_clockwise": orientation_override_degrees,
                "pre_override_rotation_degrees_clockwise": pre_override_rotation,
                "verification": override_verification,
            },
        }
    rotation_matrix, expected_size = clockwise_rotation_matrix(flat.width, flat.height, rotation_degrees)
    if upright.size != expected_size:
        raise RuntimeError("orientation rotation dimension mismatch")
    original_matrix = rotation_matrix @ base_matrix
    inverse_matrix = np.linalg.inv(original_matrix)
    metadata.update(
        {
            "output_width": upright.width,
            "output_height": upright.height,
            "quad_normalized": normalized_quad,
            "homography_from_exif_oriented_original": np.round(original_matrix, 10).tolist(),
            "inverse_homography_to_exif_oriented_original": np.round(inverse_matrix, 10).tolist(),
            "fallback_used": fallback_used,
            "orientation": orientation,
        }
    )
    return _encode_png(upright), metadata


def _content_bbox(gray: np.ndarray) -> list[int] | None:
    ink = gray < 220
    ys, xs = np.where(ink)
    if len(xs) == 0:
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)]


def _perceptual_hash(gray: np.ndarray) -> str:
    """Return a small deterministic average hash for same-student duplicate pages."""
    thumbnail = Image.fromarray(gray, mode="L").resize((16, 16), Image.Resampling.BILINEAR)
    values = np.asarray(thumbnail, dtype=np.uint8)
    mean = float(values.mean())
    bits = "".join("1" if int(value) < mean else "0" for value in values.flat)
    return f"{int(bits, 2):064x}"


def analyze_image_bytes(data: bytes, *, max_pixels: int = MAX_DEFAULT_PIXELS) -> dict[str, Any]:
    image, exif_orientation, original_size = _open_image(
        data,
        max_pixels=max_pixels,
        working_max_side=WORKING_MAX_SIDE,
    )
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    working_height, working_width = gray.shape
    width, height = original_size
    short_side = min(width, height)
    long_side = max(width, height)
    area = width * height
    dark_ratio = float(np.mean(gray < 220))
    flags: list[str] = []
    if short_side < 600:
        flags.append("very_low_resolution")
    elif short_side < 900:
        flags.append("low_resolution")
    if area >= 16_000_000 or long_side >= 4000:
        flags.append("large_canvas")
    if long_side / short_side > 2:
        flags.append("wide_or_narrow_crop")
    if float(gray.mean()) < 90 or float(gray.max()) - float(gray.min()) > 240:
        flags.append("lighting_issue")
    return {
        "width": width,
        "height": height,
        "short_side": short_side,
        "long_side": long_side,
        "area": area,
        "aspect_ratio": round(long_side / short_side, 4),
        "mean_brightness": round(float(gray.mean()), 4),
        "dark_pixel_ratio": round(dark_ratio, 6),
        "content_bbox": _scale_bbox(_content_bbox(gray), original_size, (working_width, working_height)),
        "perceptual_hash": _perceptual_hash(gray),
        "is_near_blank": dark_ratio < 0.005,
        "exif_orientation": exif_orientation,
        "working_width": working_width,
        "working_height": working_height,
        "flags": flags,
    }


def _scale_bbox(
    bbox: list[int] | None,
    source_size: tuple[int, int],
    working_size: tuple[int, int],
) -> list[int] | None:
    if bbox is None:
        return None
    source_width, source_height = source_size
    working_width, working_height = working_size
    if working_width <= 0 or working_height <= 0:
        return None
    scaled = [
        int(np.floor(bbox[0] * source_width / working_width)),
        int(np.floor(bbox[1] * source_height / working_height)),
        int(np.ceil(bbox[2] * source_width / working_width)),
        int(np.ceil(bbox[3] * source_height / working_height)),
    ]
    return [
        max(0, min(source_width, scaled[0])),
        max(0, min(source_height, scaled[1])),
        max(0, min(source_width, scaled[2])),
        max(0, min(source_height, scaled[3])),
    ]


def _enhance(image: Image.Image) -> Image.Image:
    enhanced = ImageOps.autocontrast(image.convert("RGB"), cutoff=0.5)
    return ImageEnhance.Contrast(enhanced).enhance(1.05)


def materialize_image_variants(
    data: bytes,
    output_dir: Path | str,
    *,
    max_pixels: int = MAX_DEFAULT_PIXELS,
) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_bytes, metadata = rectify_document_bytes(data, max_pixels=max_pixels)
    normalized, _, _ = _open_image(normalized_bytes, max_pixels=max_pixels)
    enhanced_bytes = _encode_png(_enhance(normalized))
    quality = analyze_image_bytes(data, max_pixels=max_pixels)
    rectified_quality = analyze_image_bytes(normalized_bytes, max_pixels=max_pixels)
    quality.update(
        {
            "normalization": metadata,
            "geometry": metadata,
            "rectified_quality": rectified_quality,
            "provenance": {"original_retained": True, "agent_primary_view": "rectified"},
        }
    )

    original_path = output_dir / "original.png"
    rectified_path = output_dir / "rectified.png"
    normalized_path = output_dir / "normalized.png"
    enhanced_path = output_dir / "enhanced.png"
    quality_path = output_dir / "quality.json"
    atomic_write_bytes(original_path, data)
    atomic_write_bytes(rectified_path, normalized_bytes)
    atomic_write_bytes(normalized_path, normalized_bytes)
    atomic_write_bytes(enhanced_path, enhanced_bytes)
    atomic_write_json(quality_path, quality)
    return {
        "original": original_path,
        "rectified": rectified_path,
        "normalized": normalized_path,
        "enhanced": enhanced_path,
        "quality": quality_path,
    }


def apply_multimodal_orientation_correction(
    variants: dict[str, Path],
    *,
    rotation_degrees_clockwise: int,
    confidence: float,
) -> dict[str, Any]:
    """Apply a rare PageObserver orientation correction and preserve transforms."""

    degrees = int(rotation_degrees_clockwise)
    if degrees not in {90, 180, 270}:
        raise ValueError("multimodal orientation correction must be 90, 180, or 270 degrees")
    quality_path = Path(variants["quality"])
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    normalized_bytes = Path(variants["normalized"]).read_bytes()
    image, _, _ = _open_image(normalized_bytes, max_pixels=MAX_DEFAULT_PIXELS)
    rotation_matrix, expected_size = clockwise_rotation_matrix(image.width, image.height, degrees)
    upright = rotate_clockwise(image, degrees)
    if upright.size != expected_size:
        raise RuntimeError("multimodal orientation rotation dimension mismatch")
    upright_bytes = _encode_png(upright)

    geometry = dict(quality.get("geometry") or {})
    old_matrix = np.asarray(geometry.get("homography_from_exif_oriented_original"), dtype=np.float64)
    if old_matrix.shape != (3, 3):
        old_matrix = np.eye(3, dtype=np.float64)
    combined = rotation_matrix @ old_matrix
    geometry.update(
        {
            "output_width": upright.width,
            "output_height": upright.height,
            "homography_from_exif_oriented_original": np.round(combined, 10).tolist(),
            "inverse_homography_to_exif_oriented_original": np.round(np.linalg.inv(combined), 10).tolist(),
            "orientation": {
                **dict(geometry.get("orientation") or {}),
                "applied": True,
                "reason": "multimodal_page_observer_fallback",
                "rotation_degrees_clockwise": degrees,
                "confidence": round(float(confidence), 6),
                "source": "page_observer",
            },
        }
    )
    quality.update(
        {
            "normalization": geometry,
            "geometry": geometry,
            "rectified_quality": analyze_image_bytes(upright_bytes),
        }
    )
    atomic_write_bytes(Path(variants["rectified"]), upright_bytes)
    atomic_write_bytes(Path(variants["normalized"]), upright_bytes)
    atomic_write_bytes(Path(variants["enhanced"]), _encode_png(_enhance(upright)))
    atomic_write_json(quality_path, quality)
    return quality
