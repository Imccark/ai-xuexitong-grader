from __future__ import annotations

import hashlib
import json

from PIL import Image

from tools.layout.audit_layout_source_dataset import audit_source_dataset


def _jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_source_audit_verifies_public_private_image_binding(workspace_tmp_path) -> None:
    dataset = workspace_tmp_path / "dataset"
    images = dataset / "images"
    images.mkdir(parents=True)
    page_id = "a" * 64
    image_path = images / f"{page_id}.png"
    Image.new("RGB", (80, 100), "white").save(image_path)
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    public = {
        "page_id": page_id,
        "assignment_id": "week",
        "student_hash": "b" * 64,
        "rectified_image_ref": f"images/{page_id}.png",
        "rectified_sha256": digest,
        "rectified_width": 80,
        "rectified_height": 100,
    }
    private = {**public, "image_sha256": digest, "source_path": str(image_path.resolve())}
    manifest = dataset / "manifest.jsonl"
    private_manifest = workspace_tmp_path / "private.jsonl"
    orientation = dataset / "orientation.json"
    _jsonl(manifest, [public])
    _jsonl(private_manifest, [private])
    orientation.write_text(json.dumps({"pages": 1, "residual_nonzero_pages": 0}), encoding="utf-8")

    report = audit_source_dataset(manifest, private_manifest, orientation, expected_pages=1, workers=1)

    assert report["ready_for_online_labeling"] is True
    assert report["verified_images"] == 1
    assert report["failures"] == []
