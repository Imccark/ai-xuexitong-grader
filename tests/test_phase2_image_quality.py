from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.grading_graph.nodes.ingest import IngestLimits, build_ingest_manifest
from app.grading_graph.nodes.image_quality import (
    WORKING_MAX_PIXELS,
    analyze_image_bytes,
    materialize_image_variants,
    normalize_image_bytes,
    rectify_document_bytes,
)


def _png_bytes(*, size: tuple[int, int] = (800, 600), exif_orientation: int | None = None, ink: bool = True) -> bytes:
    image = Image.new("RGB", size, "white")
    if ink:
        draw = ImageDraw.Draw(image)
        draw.rectangle((size[0] // 4, size[1] // 4, size[0] // 2, size[1] // 2), fill="black")
    output = io.BytesIO()
    kwargs = {}
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[274] = exif_orientation
        kwargs["exif"] = exif.tobytes()
    image.save(output, format="PNG", **kwargs)
    return output.getvalue()


def test_exif_orientation_is_normalized_without_losing_original() -> None:
    source = _png_bytes(size=(300, 800), exif_orientation=6)
    normalized, metadata = normalize_image_bytes(source)
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.size == (800, 300)
    assert metadata["exif_orientation"] == 6
    assert metadata["orientation_applied"] is True


def test_quality_gate_reports_blank_content_bbox_and_working_copy(workspace_tmp_path: Path) -> None:
    source = _png_bytes(size=(5000, 4000))
    profile = analyze_image_bytes(source)
    assert profile["area"] == 20_000_000
    assert "large_canvas" in profile["flags"]
    assert profile["content_bbox"] is not None
    assert len(profile["perceptual_hash"]) == 64
    assert profile["working_width"] * profile["working_height"] <= WORKING_MAX_PIXELS

    blank_profile = analyze_image_bytes(_png_bytes(ink=False))
    assert blank_profile["is_near_blank"] is True

    variants = materialize_image_variants(source, workspace_tmp_path / "variants")
    assert variants["original"].exists()
    assert variants["rectified"].exists()
    assert variants["normalized"].exists()
    assert variants["enhanced"].exists()
    assert variants["quality"].exists()
    with Image.open(variants["normalized"]) as image:
        assert max(image.size) <= 3200


def test_document_quad_is_rectified_and_transform_is_auditable() -> None:
    import cv2
    import numpy as np

    canvas = np.full((900, 1200, 3), 45, dtype=np.uint8)
    quad = np.asarray([[170, 100], [1030, 160], [1100, 790], [90, 830]], dtype=np.int32)
    cv2.fillConvexPoly(canvas, quad, (248, 248, 248))
    for offset in range(220, 760, 90):
        cv2.line(canvas, (170, offset), (1010, offset + 20), (35, 35, 35), 8)
    ok, encoded = cv2.imencode(".png", cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
    assert ok

    rectified, metadata = rectify_document_bytes(encoded.tobytes())
    assert metadata["detected"] is True
    assert metadata["applied"] is True
    assert metadata["fallback_used"] is False
    assert len(metadata["quad_normalized"]) == 4
    assert len(metadata["homography_from_exif_oriented_original"]) == 3
    assert len(metadata["inverse_homography_to_exif_oriented_original"]) == 3
    with Image.open(io.BytesIO(rectified)) as image:
        assert image.width > 700
        assert image.height > 500
        assert image.width * image.height < 1200 * 900


def test_rectification_falls_back_instead_of_warping_without_a_page() -> None:
    source = _png_bytes(size=(800, 600), ink=False)
    rectified, metadata = rectify_document_bytes(source)
    assert metadata["applied"] is False
    assert metadata["fallback_used"] is True
    assert metadata["homography_from_exif_oriented_original"] == [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]
    assert metadata["orientation"]["reason"] == "near_blank_orientation_skipped"
    with Image.open(io.BytesIO(rectified)) as image:
        assert image.size == (800, 600)


def test_ingest_rejects_zip_path_traversal_and_file_limits(workspace_tmp_path: Path) -> None:
    archive_path = workspace_tmp_path / "submission.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.txt", "bad")

    with pytest.raises(ValueError, match="path traversal"):
        build_ingest_manifest(archive_path)

    many_files = workspace_tmp_path / "many.zip"
    with zipfile.ZipFile(many_files, "w") as archive:
        archive.writestr("page_1.png", b"1")
        archive.writestr("page_2.png", b"2")
    with pytest.raises(ValueError, match="file count"):
        build_ingest_manifest(many_files, limits=IngestLimits(max_files=1))


def test_ingest_identifies_pdf_image_docx_and_rejects_deep_nested_archives(workspace_tmp_path: Path) -> None:
    archive_path = workspace_tmp_path / "mixed.zip"
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as inner:
        inner.writestr("page.png", b"image")
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("answer.pdf", b"pdf")
        archive.writestr("photo.jpg", b"jpg")
        archive.writestr("submission.docx", b"docx")
        archive.writestr("nested.zip", nested.getvalue())

    manifest = build_ingest_manifest(archive_path, limits=IngestLimits(max_zip_depth=1))
    assert {entry["kind"] for entry in manifest["files"]} >= {"pdf", "image", "docx", "archive"}
    assert manifest["supported_file_count"] == 4
    assert manifest["source_sha256"]

    with pytest.raises(ValueError, match="nested archive depth"):
        build_ingest_manifest(archive_path, limits=IngestLimits(max_zip_depth=0))
