from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

import prepare_rectified_labeling_images as labeling
from prepare_rectified_labeling_images import prepare_rectified_dataset


def test_prepare_rectified_dataset_uses_opaque_names_and_private_paths(workspace_tmp_path: Path) -> None:
    source = workspace_tmp_path / "student-name" / "page_1.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (500, 700), "white").save(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    private_manifest = workspace_tmp_path / "private.jsonl"
    row = {
        "page_id": "a" * 64,
        "assignment_id": "week-hash-only",
        "student_hash": "b" * 64,
        "page": 1,
        "image_sha256": source_sha,
        "width": 500,
        "height": 700,
        "source_path": str(source),
        "identity_upload_authorized": True,
    }
    private_manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
    output = workspace_tmp_path / "dataset"
    next_private = workspace_tmp_path / "next-private.jsonl"
    report = prepare_rectified_dataset(private_manifest, output, next_private, max_pages=1)

    assert report["completed_pages"] == 1
    assert (output / "images" / f"{'a' * 64}.png").is_file()
    public_text = (output / "manifest.jsonl").read_text(encoding="utf-8")
    assert "student-name" not in public_text
    assert "source_path" not in public_text
    private_row = json.loads(next_private.read_text(encoding="utf-8"))
    assert private_row["geometry_preprocessed"] is True
    assert Path(private_row["source_path"]).is_file()

    resumed = prepare_rectified_dataset(private_manifest, output, next_private, max_pages=1, workers=2)
    assert resumed["resumed_pages"] == 1
    assert resumed["newly_processed_pages"] == 0


def test_new_orientation_override_invalidates_cached_page(workspace_tmp_path: Path, monkeypatch) -> None:
    source = workspace_tmp_path / "page.png"
    Image.new("RGB", (20, 30), "white").save(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    page_id = "c" * 64
    manifest = workspace_tmp_path / "private.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "page_id": page_id,
                "student_hash": "d" * 64,
                "image_sha256": source_sha,
                "source_path": str(source),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_rectify(data: bytes, *, orientation_override_degrees: int = 0):
        with Image.open(source) as image:
            output = image.transpose(Image.Transpose.ROTATE_270) if orientation_override_degrees == 90 else image.copy()
            from io import BytesIO

            buffer = BytesIO()
            output.save(buffer, format="PNG")
        orientation = {
            "available": True,
            "reason": "teacher_rotation_override" if orientation_override_degrees else "near_blank_orientation_skipped",
            "rotation_degrees_clockwise": orientation_override_degrees,
        }
        if orientation_override_degrees:
            orientation["teacher_override"] = {"additional_rotation_degrees_clockwise": orientation_override_degrees}
        return buffer.getvalue(), {
            "version": labeling.RECTIFICATION_VERSION,
            "applied": False,
            "output_width": output.width,
            "output_height": output.height,
            "orientation": orientation,
        }

    monkeypatch.setattr(labeling, "rectify_document_bytes", fake_rectify)
    output = workspace_tmp_path / "dataset"
    private_output = workspace_tmp_path / "rectified-private.jsonl"
    first = prepare_rectified_dataset(manifest, output, private_output, max_pages=1)
    first_private = json.loads(private_output.read_text(encoding="utf-8"))
    override = {
        "page_id": page_id,
        "rotation_degrees_clockwise": 90,
        "input_image_sha256": first_private["image_sha256"],
    }
    second = prepare_rectified_dataset(
        manifest,
        output,
        private_output,
        max_pages=1,
        orientation_overrides={page_id: override},
    )

    assert first["newly_processed_pages"] == 1
    assert second["resumed_pages"] == 0
    assert second["orientation_overrides_applied"] == 1
    public = json.loads((output / "manifest.jsonl").read_text(encoding="utf-8"))
    assert public["orientation_override"] == override
    assert public["rectified_width"] == 30
    assert public["rectified_height"] == 20
