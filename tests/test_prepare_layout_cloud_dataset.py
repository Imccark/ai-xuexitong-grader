from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from PIL import Image

from tools.evaluation.core.layout_teacher import CONSENSUS_VERSION, LABELING_VERSION, QUALITY_VERSION
from tools.layout.prepare_layout_cloud_dataset import (
    DEFAULT_ALIAS_MAP,
    DEFAULT_EXPECTED_SOURCE_PAGES,
    DEFAULT_IMAGES,
    DEFAULT_LABELS,
    DEFAULT_MANIFEST,
    DEFAULT_OUTPUT,
    DEFAULT_PRIVATE_SPLIT,
    DEFAULT_VERSION,
    audit_alias_coverage,
    assign_splits,
    build_dataset,
    clean_records,
)


def test_cloud_packaging_defaults_require_current_full_v4_coverage() -> None:
    assert DEFAULT_LABELS.as_posix().endswith("evaluation/layout_labels/all_rectified_v4_unique_results/labels.jsonl")
    assert DEFAULT_MANIFEST.as_posix().endswith("datasets/layout_all_v4/manifest.jsonl")
    assert DEFAULT_IMAGES.as_posix().endswith("datasets/layout_all_v4/images")
    assert DEFAULT_ALIAS_MAP.as_posix().endswith("runtime_logs/teacher_labeling/all_rectified_v4_aliases_private.jsonl")
    assert DEFAULT_OUTPUT.as_posix().endswith("datasets/layout_training_v2_full")
    assert DEFAULT_PRIVATE_SPLIT.as_posix().endswith(
        "runtime_logs/teacher_labeling/layout_training_v2_full_private_split.jsonl"
    )
    assert DEFAULT_VERSION == "layout-silver-v2-full2287"
    assert DEFAULT_EXPECTED_SOURCE_PAGES == 2287


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _fixture(workspace_tmp_path: Path, pages: int = 30):
    images = workspace_tmp_path / "source" / "images"
    images.mkdir(parents=True)
    manifest = []
    labels = []
    for index in range(pages):
        page_id = f"{index + 1:064x}"
        student_hash = f"{index // 2 + 1000:064x}"
        image_path = images / f"{page_id}.png"
        random_bytes = random.Random(index).randbytes((100 + index) * 120 * 3)
        image = Image.frombytes("RGB", (100 + index, 120), random_bytes)
        image.save(image_path)
        import hashlib

        image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
        manifest.append(
            {
                "page_id": page_id,
                "assignment_id": f"week-{index % 5}",
                "student_hash": student_hash,
                "page": index + 1,
                "rectified_sha256": image_hash,
            }
        )
        labels.append(
            {
                **manifest[-1],
                "rectified_width": 100 + index,
                "rectified_height": 120,
                "consensus": {"status": "high_confidence_silver"},
                "teacher": {
                    "labeling_version": LABELING_VERSION,
                    "consensus_version": CONSENSUS_VERSION,
                    "quality_version": QUALITY_VERSION,
                },
                "final_layout": {
                    "rotation_degrees_clockwise": 0,
                    "regions": [
                        {
                            "region_id": "q1",
                            "parent_region_id": "",
                            "region_type": "question_block",
                            "question_label": "1",
                            "bbox": [0.1, 0.1, 0.9, 0.9],
                            "reading_order": 1,
                            "confidence": 0.95,
                        }
                    ],
                },
            }
        )
    manifest_path = workspace_tmp_path / "manifest.jsonl"
    labels_path = workspace_tmp_path / "labels.jsonl"
    _write_jsonl(manifest_path, manifest)
    _write_jsonl(labels_path, labels)
    return labels_path, manifest_path, images


def test_clean_and_split_prevents_student_and_near_duplicate_leakage(workspace_tmp_path) -> None:
    labels, manifest, images = _fixture(workspace_tmp_path)
    records, report = clean_records(labels, manifest, images)
    splits, split_report = assign_splits(records, seed="test-seed", near_duplicate_distance=-1)

    assert report["pages"] == 30
    assert set(splits.values()) == {"train", "validation", "hidden_test"}
    by_student = {}
    for row in records:
        by_student.setdefault(row["student_hash"], set()).add(splits[row["page_id"]])
    assert all(len(values) == 1 for values in by_student.values())
    assert split_report["leakage"] == {"student": [], "exact_image": [], "near_duplicate": []}


def test_clean_records_rejects_quality_quarantine(workspace_tmp_path) -> None:
    labels_path, manifest_path, images = _fixture(workspace_tmp_path, pages=1)
    labels = [json.loads(line) for line in labels_path.read_text(encoding="utf-8").splitlines()]
    labels[0]["training_eligible"] = False
    labels[0]["consensus"]["unresolved_quality_flags"] = [
        {"kind": "answerless_label_candidate", "region_id": "q1"}
    ]
    _write_jsonl(labels_path, labels)

    with pytest.raises(ValueError, match="quality-quarantined"):
        clean_records(labels_path, manifest_path, images)


def test_build_dataset_emits_paddlex_coco_and_private_map_stays_outside(workspace_tmp_path) -> None:
    labels, manifest, images = _fixture(workspace_tmp_path)
    records, _report = clean_records(labels, manifest, images)
    alias_rows = [
        {
            "page_id": row["page_id"],
            "canonical_page_id": row["page_id"],
            "image_sha256": row["image_sha256"],
            "is_canonical": True,
        }
        for row in records
    ]
    alias_rows.append(
        {
            "page_id": "f" * 64,
            "canonical_page_id": records[0]["page_id"],
            "image_sha256": records[0]["image_sha256"],
            "is_canonical": False,
        }
    )
    alias_map = workspace_tmp_path / "private" / "aliases.jsonl"
    _write_jsonl(alias_map, alias_rows)
    output = workspace_tmp_path / "layout_training_v1"
    private = workspace_tmp_path / "private" / "split.jsonl"
    report = build_dataset(
        labels_path=labels,
        manifest_path=manifest,
        images_dir=images,
        output_dir=output,
        private_split_path=private,
        version="test-v1",
        alias_map_path=alias_map,
        expected_source_pages=31,
    )

    assert report["upload_ready"] is True
    assert report["quality_version"] == QUALITY_VERSION
    assert report["source_coverage"] == {
        "source_pages": 31,
        "labeled_unique_pages": 30,
        "exact_alias_pages": 1,
        "all_source_pages_covered": True,
        "private_alias_map_uploaded": False,
    }
    assert Path(report["archive"]).is_file()
    train = json.loads((output / "annotations" / "instance_train.json").read_text(encoding="utf-8"))
    assert train["images"]
    assert train["annotations"]
    assert all("read_order" in row and len(row["segmentation"][0]) == 8 for row in train["annotations"])
    assert "student_hash" not in (output / "manifest.jsonl").read_text(encoding="utf-8")
    assert "student_hash" in private.read_text(encoding="utf-8")
    assert "aliases.jsonl" not in {path.name for path in output.rglob("*")}


def test_alias_coverage_rejects_unlabeled_canonical_reference() -> None:
    records = [{"page_id": "a" * 64, "image_sha256": "b" * 64}]
    aliases = [
        {
            "page_id": "c" * 64,
            "canonical_page_id": "d" * 64,
            "image_sha256": "b" * 64,
            "is_canonical": False,
        }
    ]

    with pytest.raises(ValueError, match="unlabeled canonical"):
        audit_alias_coverage(records, aliases)
