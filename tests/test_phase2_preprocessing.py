from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from grading_graph.preprocessing import dry_run_processed_image_quality, dry_run_raw_submissions
from run_preprocessing import build_candidates_from_raw, preprocess_one_student


def _png_bytes() -> bytes:
    image = Image.new("RGB", (20, 20), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_preprocessing_dry_run_is_read_only_and_reports_archive_status(workspace_tmp_path) -> None:
    raw_dir = workspace_tmp_path / "第一周" / "raw_submissions"
    raw_dir.mkdir(parents=True)
    archive_path = raw_dir / "student-1.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("answer.png", b"not-an-image")
    archive_path.write_bytes(buffer.getvalue())
    report = dry_run_raw_submissions(workspace_tmp_path)
    assert report["submission_count"] == 1
    assert report["mutated_processed_or_results"] is False
    assert report["records"][0]["student_hash"]
    assert not (workspace_tmp_path / "第一周" / "processed_images").exists()


def test_processed_quality_dry_run_is_read_only(workspace_tmp_path) -> None:
    from PIL import Image

    page_dir = workspace_tmp_path / "第一周" / "processed_images" / "student-1"
    page_dir.mkdir(parents=True)
    Image.new("RGB", (20, 20), "white").save(page_dir / "page_1.png")
    (page_dir / "page_1.png").replace(page_dir / "page_2.png")
    Image.new("RGB", (20, 20), "white").save(page_dir / "page_1.png")
    report = dry_run_processed_image_quality(workspace_tmp_path)
    assert report["page_count"] == 2
    assert report["failed_count"] == 0
    assert report["duplicate_group_count"] == 1
    assert report["duplicate_page_count"] == 2
    assert report["working_copy_bounded"] is True
    assert report["mutated_processed_or_results"] is False


def test_reprocess_failure_keeps_existing_pages_and_success_publishes_staging(workspace_tmp_path: Path) -> None:
    raw_dir = workspace_tmp_path / "raw"
    processed_dir = workspace_tmp_path / "processed"
    temp_root = workspace_tmp_path / "temp"
    backup_root = workspace_tmp_path / "backups"
    raw_dir.mkdir(parents=True)
    target = processed_dir / "student-1"
    target.mkdir(parents=True)
    old_page = target / "page_1.png"
    old_page.write_bytes(b"old-page")

    bad_zip = raw_dir / "student-1.zip"
    with zipfile.ZipFile(bad_zip, "w") as archive:
        archive.writestr("answer.png", b"not-an-image")
    failed = preprocess_one_student(bad_zip, processed_dir, temp_root, True, backup_root)
    assert failed.status == "failed"
    assert old_page.read_bytes() == b"old-page"

    with zipfile.ZipFile(bad_zip, "w") as archive:
        archive.writestr("answer.png", _png_bytes())
    succeeded = preprocess_one_student(bad_zip, processed_dir, temp_root, True, backup_root)
    assert succeeded.status == "success"
    assert old_page.read_bytes() != b"old-page"
    assert list(backup_root.glob("student-1-*"))


def test_actual_preprocessing_applies_exif_orientation_to_standard_page(workspace_tmp_path: Path) -> None:
    image = Image.new("RGB", (20, 40), "white")
    exif = Image.Exif()
    exif[274] = 6
    source = io.BytesIO()
    image.save(source, format="JPEG", exif=exif.tobytes())

    raw_zip = workspace_tmp_path / "student-rotation.zip"
    with zipfile.ZipFile(raw_zip, "w") as archive:
        archive.writestr("answer.jpg", source.getvalue())

    result = preprocess_one_student(
        raw_zip,
        workspace_tmp_path / "processed",
        workspace_tmp_path / "temp",
        True,
        workspace_tmp_path / "backups",
    )
    assert result.status == "success"
    with Image.open(workspace_tmp_path / "processed" / "student-rotation" / "page_1.png") as output:
        assert output.size == (40, 20)


def test_actual_preprocessing_rejects_archive_path_traversal(workspace_tmp_path: Path) -> None:
    archive_path = workspace_tmp_path / "student-1.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../outside.png", _png_bytes())
    try:
        build_candidates_from_raw(archive_path)
    except ValueError as exc:
        assert "path traversal" in str(exc)
    else:
        raise AssertionError("unsafe archive member was accepted")


@pytest.mark.parametrize("member_name", ["folder/../outside.png", r"folder\\..\\outside.png"])
def test_archive_path_traversal_is_rejected_before_normalization(workspace_tmp_path: Path, member_name: str) -> None:
    archive_path = workspace_tmp_path / "student-2.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(member_name, _png_bytes())
    with pytest.raises(ValueError, match="path traversal"):
        build_candidates_from_raw(archive_path)
