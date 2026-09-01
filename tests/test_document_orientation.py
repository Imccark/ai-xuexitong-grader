from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw

from app.grading_graph.nodes import image_quality
from app.grading_graph.nodes.orientation import clockwise_rotation_matrix, rotate_clockwise


def test_clockwise_rotation_matrices_round_trip_pixel_corners() -> None:
    width, height = 11, 7
    corners = np.asarray([[0, 0, 1], [width - 1, 0, 1], [width - 1, height - 1, 1], [0, height - 1, 1]], dtype=float).T
    for degrees in (0, 90, 180, 270):
        matrix, output_size = clockwise_rotation_matrix(width, height, degrees)
        mapped = matrix @ corners
        restored = np.linalg.inv(matrix) @ mapped
        assert np.allclose(restored, corners)
        expected = (width, height) if degrees in {0, 180} else (height, width)
        assert output_size == expected


def test_rectification_applies_accepted_orientation_and_composes_transform(monkeypatch) -> None:
    image = Image.new("RGB", (500, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 480, 280), outline="black", width=8)
    draw.text((60, 120), "HOMEWORK 1 - 2 = -1", fill="black")
    payload = io.BytesIO()
    image.save(payload, format="PNG")

    monkeypatch.setattr(image_quality, "_detect_document_quad", lambda _rgb: (None, {"detected": False, "applied": False, "reason": "synthetic"}))
    results = iter(
        [
            {
                "available": True,
                "applied": True,
                "reason": "accepted",
                "rotation_degrees_clockwise": 90,
                "predicted_orientation_degrees_clockwise": 270,
                "confidence": 0.99,
                "margin": 0.95,
                "orientation_scores": {"0": 0.0, "90": 0.01, "180": 0.0, "270": 0.99},
                "model": "fake",
                "model_sha256": "fake",
            },
            {
                "available": True,
                "applied": False,
                "reason": "accepted",
                "rotation_degrees_clockwise": 0,
                "predicted_orientation_degrees_clockwise": 0,
                "confidence": 0.99,
                "margin": 0.95,
                "orientation_scores": {"0": 0.99, "90": 0.01, "180": 0.0, "270": 0.0},
                "model": "fake",
                "model_sha256": "fake",
            },
        ]
    )
    monkeypatch.setattr(image_quality, "classify_document_orientation", lambda _image: next(results))
    rectified, metadata = image_quality.rectify_document_bytes(payload.getvalue())
    with Image.open(io.BytesIO(rectified)) as output:
        assert output.size == (300, 500)
    assert metadata["orientation"]["rotation_degrees_clockwise"] == 90
    assert metadata["orientation"]["verification"]["rotation_degrees_clockwise"] == 0
    matrix = np.asarray(metadata["homography_from_exif_oriented_original"], dtype=float)
    inverse = np.asarray(metadata["inverse_homography_to_exif_oriented_original"], dtype=float)
    assert np.allclose(matrix @ inverse, np.eye(3), atol=1e-7)


def test_rectification_reverts_an_unstable_orientation_cycle(monkeypatch) -> None:
    image = Image.new("RGB", (500, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 480, 280), outline="black", width=8)
    draw.text((60, 120), "HOMEWORK", fill="black")
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    contradictory = {
        "available": True,
        "applied": True,
        "reason": "accepted",
        "rotation_degrees_clockwise": 180,
        "predicted_orientation_degrees_clockwise": 180,
        "confidence": 0.9,
        "margin": 0.8,
        "orientation_scores": {"0": 0.05, "90": 0.02, "180": 0.9, "270": 0.03},
        "model": "fake",
        "model_sha256": "fake",
    }
    monkeypatch.setattr(image_quality, "_detect_document_quad", lambda _rgb: (None, {"detected": False, "applied": False, "reason": "synthetic"}))
    monkeypatch.setattr(image_quality, "classify_document_orientation", lambda _image: contradictory.copy())

    rectified, metadata = image_quality.rectify_document_bytes(payload.getvalue())

    with Image.open(io.BytesIO(rectified)) as output:
        assert output.size == image.size
    assert metadata["orientation"]["reason"] == "unstable_orientation_cycle"
    assert metadata["orientation"]["rotation_degrees_clockwise"] == 0
    assert metadata["orientation"]["verification"]["rotation_degrees_clockwise"] == 180


def test_rectification_resolves_low_confidence_rotation_by_upright_candidate_search(monkeypatch) -> None:
    image = Image.new("RGB", (500, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 480, 280), outline="black", width=8)
    draw.text((60, 120), "HOMEWORK 1 - 2 = -1", fill="black")
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    low_initial = {
        "available": True,
        "applied": False,
        "reason": "low_orientation_confidence",
        "rotation_degrees_clockwise": 0,
        "predicted_orientation_degrees_clockwise": 90,
        "confidence": 0.60,
        "margin": 0.30,
        "orientation_scores": {"0": 0.07, "90": 0.60, "180": 0.03, "270": 0.30},
        "model": "fake",
        "model_sha256": "fake",
    }
    upright = {
        **low_initial,
        "reason": "accepted",
        "predicted_orientation_degrees_clockwise": 0,
        "confidence": 0.92,
        "margin": 0.88,
        "orientation_scores": {"0": 0.92, "90": 0.03, "180": 0.03, "270": 0.02},
    }
    weak_180 = {
        **low_initial,
        "orientation_scores": {"0": 0.04, "90": 0.88, "180": 0.04, "270": 0.04},
    }
    upside_down = {
        **low_initial,
        "predicted_orientation_degrees_clockwise": 180,
        "confidence": 0.82,
        "margin": 0.70,
        "orientation_scores": {"0": 0.12, "90": 0.03, "180": 0.82, "270": 0.03},
    }
    results = iter([low_initial, upright, weak_180, upside_down, upright])
    monkeypatch.setattr(image_quality, "_detect_document_quad", lambda _rgb: (None, {"detected": False}))
    monkeypatch.setattr(image_quality, "classify_document_orientation", lambda _image: next(results))

    rectified, metadata = image_quality.rectify_document_bytes(payload.getvalue())

    with Image.open(io.BytesIO(rectified)) as output:
        assert output.size == (300, 500)
    orientation = metadata["orientation"]
    assert orientation["reason"] == "accepted_by_upright_candidate_search"
    assert orientation["rotation_degrees_clockwise"] == 90
    assert orientation["candidate_search"]["selected_upright_confidence"] == 0.92
    assert orientation["verification"]["predicted_orientation_degrees_clockwise"] == 0


def test_candidate_search_rejects_moderate_false_positive(monkeypatch) -> None:
    image = Image.new("RGB", (500, 300), "white")
    initial = {
        "available": True,
        "applied": False,
        "reason": "low_orientation_confidence",
        "rotation_degrees_clockwise": 0,
        "predicted_orientation_degrees_clockwise": 180,
        "confidence": 0.59,
        "margin": 0.25,
        "orientation_scores": {"0": 0.35, "90": 0.03, "180": 0.59, "270": 0.03},
    }
    moderate_upright = {
        **initial,
        "reason": "accepted",
        "predicted_orientation_degrees_clockwise": 0,
        "confidence": 0.86,
        "orientation_scores": {"0": 0.86, "90": 0.05, "180": 0.05, "270": 0.04},
    }
    weak = {**initial, "orientation_scores": {"0": 0.03, "90": 0.80, "180": 0.10, "270": 0.07}}
    results = iter([weak, moderate_upright, weak])
    monkeypatch.setattr(image_quality, "classify_document_orientation", lambda _image: next(results))

    resolved = image_quality._resolve_ambiguous_orientation(image, initial)

    assert resolved["rotation_degrees_clockwise"] == 0
    assert resolved["reason"] == "low_orientation_confidence"
    assert resolved["candidate_search"]["accepted"] is False
    assert resolved["candidate_search"]["selected_upright_confidence"] == 0.86


def test_teacher_rotation_override_composes_after_local_preprocessing(monkeypatch) -> None:
    image = Image.new("RGB", (500, 300), "white")
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    upright = {
        "available": True,
        "applied": False,
        "reason": "accepted",
        "rotation_degrees_clockwise": 0,
        "predicted_orientation_degrees_clockwise": 0,
        "confidence": 0.95,
        "margin": 0.90,
        "orientation_scores": {"0": 0.95, "90": 0.02, "180": 0.02, "270": 0.01},
    }
    monkeypatch.setattr(image_quality, "_detect_document_quad", lambda _rgb: (None, {"detected": False}))
    monkeypatch.setattr(image_quality, "classify_document_orientation", lambda _image: upright)

    rectified, metadata = image_quality.rectify_document_bytes(
        payload.getvalue(),
        orientation_override_degrees=90,
    )

    with Image.open(io.BytesIO(rectified)) as output:
        assert output.size == (300, 500)
    orientation = metadata["orientation"]
    assert orientation["reason"] == "teacher_rotation_override"
    assert orientation["rotation_degrees_clockwise"] == 90
    assert orientation["teacher_override"]["additional_rotation_degrees_clockwise"] == 90
    assert orientation["teacher_override"]["verification"]["predicted_orientation_degrees_clockwise"] == 0


def test_rotate_clockwise_changes_dimensions() -> None:
    image = Image.new("RGB", (9, 5), "white")
    assert rotate_clockwise(image, 90).size == (5, 9)
    assert rotate_clockwise(image, 180).size == (9, 5)
    assert rotate_clockwise(image, 270).size == (5, 9)
