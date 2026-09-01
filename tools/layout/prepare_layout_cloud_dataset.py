from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import tarfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from tools.evaluation.core.layout_teacher import CONSENSUS_VERSION, LABELING_VERSION, QUALITY_VERSION, validate_layout


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LABELS = ROOT / "evaluation" / "layout_labels" / "all_rectified_v4_unique_results" / "labels.jsonl"
DEFAULT_MANIFEST = ROOT / "datasets" / "layout_all_v4" / "manifest.jsonl"
DEFAULT_IMAGES = ROOT / "datasets" / "layout_all_v4" / "images"
DEFAULT_ALIAS_MAP = ROOT / "runtime_logs" / "teacher_labeling" / "all_rectified_v4_aliases_private.jsonl"
DEFAULT_OUTPUT = ROOT / "datasets" / "layout_training_v2_full"
DEFAULT_PRIVATE_SPLIT = ROOT / "runtime_logs" / "teacher_labeling" / "layout_training_v2_full_private_split.jsonl"
DEFAULT_VERSION = "layout-silver-v2-full2287"
DEFAULT_EXPECTED_SOURCE_PAGES = 2287
DATASET_MARKER = ".layout_cloud_dataset"
SCHEMA_VERSION = "1.0"
SPLIT_RATIOS = {"train": 0.8, "validation": 0.1, "hidden_test": 0.1}
CATEGORY_NAMES = (
    "question_block",
    "subquestion",
    "student_answer",
    "cross_page_continuation",
    "identity",
    "header_footer",
    "unknown",
)
SECRET_RE = re.compile(r"(?:^|[^A-Za-z])sk-(?:sp-)?[A-Za-z0-9._-]{12,}|Bearer\s+[A-Za-z0-9._-]{20,}")
WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\")


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}: line {line_number} must be a JSON object")
        rows.append(value)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dhash(path: Path) -> int:
    with Image.open(path) as image:
        grayscale = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(grayscale.get_flattened_data())
    result = 0
    for y in range(8):
        for x in range(8):
            result = (result << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return result


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def clean_records(labels_path: Path, manifest_path: Path, images_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    labels = _jsonl(labels_path)
    manifest = {str(row.get("page_id")): row for row in _jsonl(manifest_path)}
    if len(manifest) != len(_jsonl(manifest_path)):
        raise ValueError("manifest contains duplicate page_id values")
    if not labels:
        raise ValueError("no layout labels found")

    page_ids: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    hash_counts: Counter[str] = Counter()
    confidence_values: list[float] = []
    for row in labels:
        page_id = str(row.get("page_id") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", page_id) or page_id in page_ids:
            raise ValueError(f"invalid or duplicate page_id: {page_id!r}")
        page_ids.add(page_id)
        source = manifest.get(page_id)
        if source is None:
            raise ValueError(f"label page missing from public manifest: {page_id}")
        image_path = images_dir / f"{page_id}.png"
        if not image_path.is_file():
            raise ValueError(f"image missing: {image_path}")
        image_hash = _sha256(image_path)
        expected_hash = str(row.get("rectified_sha256") or source.get("rectified_sha256") or "")
        if image_hash != expected_hash:
            raise ValueError(f"image hash mismatch: {page_id}")
        with Image.open(image_path) as image:
            width, height = image.size
        if (width, height) != (int(row.get("rectified_width", 0)), int(row.get("rectified_height", 0))):
            raise ValueError(f"image dimensions mismatch: {page_id}")
        if str(row.get("teacher", {}).get("labeling_version")) != LABELING_VERSION:
            raise ValueError(f"wrong labeling version: {page_id}")
        if str(row.get("teacher", {}).get("consensus_version")) != CONSENSUS_VERSION:
            raise ValueError(f"wrong consensus version: {page_id}")
        if str(row.get("teacher", {}).get("quality_version")) != QUALITY_VERSION:
            raise ValueError(f"wrong quality version: {page_id}")
        if not bool(row.get("training_eligible", True)):
            raise ValueError(f"quality-quarantined page cannot enter training package: {page_id}")
        if row.get("consensus", {}).get("unresolved_quality_flags"):
            raise ValueError(f"unresolved quality flags: {page_id}")
        layout = validate_layout(row.get("final_layout"))
        if int(layout.get("rotation_degrees_clockwise", 0)) != 0:
            raise ValueError(f"non-zero final rotation: {page_id}")
        regions = sorted(layout["regions"], key=lambda item: (int(item["reading_order"]), str(item["region_id"])))
        ids = {str(region["region_id"]) for region in regions}
        if len(ids) != len(regions):
            raise ValueError(f"duplicate region_id: {page_id}")
        normalized_regions: list[dict[str, Any]] = []
        for read_order, region in enumerate(regions):
            region_type = str(region.get("region_type") or "unknown")
            if region_type not in CATEGORY_NAMES:
                raise ValueError(f"unsupported region type {region_type!r}: {page_id}")
            parent = str(region.get("parent_region_id") or "")
            if parent and parent not in ids:
                raise ValueError(f"broken parent {parent!r}: {page_id}")
            bbox = [float(value) for value in region["bbox"]]
            if not all(math.isfinite(value) for value in bbox):
                raise ValueError(f"non-finite bbox: {page_id}")
            x1, y1, x2, y2 = bbox
            if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
                raise ValueError(f"invalid normalized bbox: {page_id}")
            confidence = float(region.get("confidence", 0))
            confidence_values.append(confidence)
            category_counts[region_type] += 1
            normalized_regions.append(
                {
                    "region_id": str(region["region_id"]),
                    "parent_region_id": parent,
                    "region_type": region_type,
                    "question_label": str(region.get("question_label") or "").strip(),
                    "bbox": [round(value, 6) for value in bbox],
                    "read_order": read_order,
                    "confidence": confidence,
                    "contains_critical_minus": bool(region.get("contains_critical_minus")),
                    "continues_from_previous_page": bool(region.get("continues_from_previous_page")),
                    "continues_to_next_page": bool(region.get("continues_to_next_page")),
                }
            )
        hash_counts[image_hash] += 1
        cleaned.append(
            {
                "page_id": page_id,
                "assignment_id": str(row.get("assignment_id") or source.get("assignment_id") or ""),
                "student_hash": str(row.get("student_hash") or source.get("student_hash") or ""),
                "page": int(row.get("page") or source.get("page") or 0),
                "width": width,
                "height": height,
                "image_path": image_path,
                "image_sha256": image_hash,
                "dhash": _dhash(image_path),
                "label_status": str(row.get("consensus", {}).get("status") or ""),
                "regions": normalized_regions,
            }
        )
    if any(not re.fullmatch(r"[0-9a-f]{64}", row["student_hash"]) for row in cleaned):
        raise ValueError("one or more rows have invalid student_hash")
    report = {
        "pages": len(cleaned),
        "regions": sum(len(row["regions"]) for row in cleaned),
        "students": len({row["student_hash"] for row in cleaned}),
        "assignments": dict(sorted(Counter(row["assignment_id"] for row in cleaned).items())),
        "categories": dict(sorted(category_counts.items())),
        "exact_duplicate_hashes": sum(1 for count in hash_counts.values() if count > 1),
        "minimum_region_confidence": min(confidence_values) if confidence_values else None,
        "source_manifest_pages": len(manifest),
        "unused_manifest_pages": len(set(manifest) - page_ids),
    }
    return cleaned, report


def audit_alias_coverage(
    records: list[dict[str, Any]],
    alias_rows: list[dict[str, Any]],
    *,
    expected_source_pages: int | None = None,
) -> dict[str, Any]:
    """Prove that every source page maps to one labeled canonical image."""
    if not alias_rows:
        raise ValueError("alias coverage map is empty")
    canonical_hashes = {str(row["page_id"]): str(row["image_sha256"]) for row in records}
    source_page_ids: set[str] = set()
    canonical_rows: set[str] = set()
    alias_pages = 0
    for row in alias_rows:
        page_id = str(row.get("page_id") or "")
        canonical_page_id = str(row.get("canonical_page_id") or "")
        image_sha = str(row.get("image_sha256") or "")
        is_canonical = bool(row.get("is_canonical"))
        if not re.fullmatch(r"[0-9a-f]{64}", page_id) or page_id in source_page_ids:
            raise ValueError("alias coverage map contains an invalid or duplicate source page_id")
        if canonical_page_id not in canonical_hashes:
            raise ValueError("alias coverage map references an unlabeled canonical page")
        if image_sha != canonical_hashes[canonical_page_id]:
            raise ValueError("alias coverage image hash does not match its canonical label")
        if is_canonical != (page_id == canonical_page_id):
            raise ValueError("alias coverage canonical flag is inconsistent")
        source_page_ids.add(page_id)
        if is_canonical:
            canonical_rows.add(page_id)
        else:
            alias_pages += 1
    if canonical_rows != set(canonical_hashes):
        raise ValueError("alias coverage map does not contain exactly one row for every canonical page")
    if expected_source_pages is not None and len(source_page_ids) != expected_source_pages:
        raise ValueError(
            f"alias coverage source-page count mismatch: expected={expected_source_pages}, actual={len(source_page_ids)}"
        )
    return {
        "source_pages": len(source_page_ids),
        "labeled_unique_pages": len(canonical_hashes),
        "exact_alias_pages": alias_pages,
        "all_source_pages_covered": True,
        "private_alias_map_uploaded": False,
    }


def assign_splits(records: list[dict[str, Any]], *, seed: str, near_duplicate_distance: int = 2) -> tuple[dict[str, str], dict[str, Any]]:
    page_ids = [row["page_id"] for row in records]
    union = _UnionFind(page_ids)
    by_student: dict[str, list[str]] = defaultdict(list)
    by_hash: dict[str, list[str]] = defaultdict(list)
    for row in records:
        by_student[row["student_hash"]].append(row["page_id"])
        by_hash[row["image_sha256"]].append(row["page_id"])
    for group in [*by_student.values(), *by_hash.values()]:
        for page_id in group[1:]:
            union.union(group[0], page_id)

    near_pairs: list[dict[str, Any]] = []
    ordered = sorted(records, key=lambda row: row["page_id"])
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            distance = int(left["dhash"] ^ right["dhash"]).bit_count()
            if distance <= near_duplicate_distance:
                union.union(left["page_id"], right["page_id"])
                near_pairs.append({"left": left["page_id"], "right": right["page_id"], "distance": distance})

    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        clusters[union.find(row["page_id"])].append(row)
    category_totals = Counter(region["region_type"] for row in records for region in row["regions"])
    rare_categories = {name for name, count in category_totals.items() if count < 3}
    forced_train = {
        cluster_id
        for cluster_id, rows in clusters.items()
        if any(region["region_type"] in rare_categories for row in rows for region in row["regions"])
    }
    movable = [
        (cluster_id, rows)
        for cluster_id, rows in clusters.items()
        if cluster_id not in forced_train
    ]
    movable.sort(key=lambda item: hashlib.sha256(f"{seed}:{item[0]}".encode()).hexdigest())
    targets = {
        "hidden_test": round(len(records) * SPLIT_RATIOS["hidden_test"]),
        "validation": round(len(records) * SPLIT_RATIOS["validation"]),
    }
    cluster_split = {cluster_id: "train" for cluster_id in forced_train}
    cursor = 0
    for split in ("hidden_test", "validation"):
        assigned = 0
        while cursor < len(movable) and (assigned < targets[split] or assigned == 0):
            cluster_id, rows = movable[cursor]
            cursor += 1
            cluster_split[cluster_id] = split
            assigned += len(rows)
    for cluster_id, _rows in movable[cursor:]:
        cluster_split[cluster_id] = "train"
    page_splits = {row["page_id"]: cluster_split[union.find(row["page_id"])] for row in records}

    student_splits: dict[str, set[str]] = defaultdict(set)
    hash_splits: dict[str, set[str]] = defaultdict(set)
    for row in records:
        student_splits[row["student_hash"]].add(page_splits[row["page_id"]])
        hash_splits[row["image_sha256"]].add(page_splits[row["page_id"]])
    leaks = {
        "student": [key for key, values in student_splits.items() if len(values) > 1],
        "exact_image": [key for key, values in hash_splits.items() if len(values) > 1],
        "near_duplicate": [pair for pair in near_pairs if page_splits[pair["left"]] != page_splits[pair["right"]]],
    }
    if any(leaks.values()):
        raise ValueError(f"split leakage detected: {leaks}")
    split_counts = Counter(page_splits.values())
    if set(split_counts) != set(SPLIT_RATIOS):
        raise ValueError(f"all three splits must be non-empty: {dict(split_counts)}")
    report = {
        "seed": seed,
        "ratios": SPLIT_RATIOS,
        "page_counts": dict(sorted(split_counts.items())),
        "cluster_count": len(clusters),
        "near_duplicate_distance": near_duplicate_distance,
        "near_duplicate_pairs": near_pairs,
        "rare_categories_forced_to_train": sorted(rare_categories),
        "forced_train_clusters": len(forced_train),
        "leakage": leaks,
    }
    return page_splits, report


def _link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _coco_for_split(records: list[dict[str, Any]], split: str) -> dict[str, Any]:
    categories = [{"id": index + 1, "name": name, "supercategory": "homework_layout"} for index, name in enumerate(CATEGORY_NAMES)]
    category_ids = {item["name"]: item["id"] for item in categories}
    images: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    annotation_id = 1
    selected = sorted((row for row in records if row["split"] == split), key=lambda row: row["page_id"])
    for image_id, row in enumerate(selected, 1):
        images.append({"id": image_id, "file_name": f"{row['page_id']}.png", "width": row["width"], "height": row["height"]})
        for region in row["regions"]:
            x1, y1, x2, y2 = region["bbox"]
            px1, py1 = x1 * row["width"], y1 * row["height"]
            px2, py2 = x2 * row["width"], y2 * row["height"]
            width, height = px2 - px1, py2 - py1
            annotations.append(
                {
                    "id": annotation_id,
                    "image_id": image_id,
                    "category_id": category_ids[region["region_type"]],
                    "bbox": [round(px1, 3), round(py1, 3), round(width, 3), round(height, 3)],
                    "area": round(width * height, 3),
                    "segmentation": [[round(value, 3) for value in (px1, py1, px2, py1, px2, py2, px1, py2)]],
                    "iscrowd": 0,
                    "read_order": region["read_order"],
                    "attributes": {
                        "region_id": region["region_id"],
                        "parent_region_id": region["parent_region_id"],
                        "question_label": region["question_label"],
                        "contains_critical_minus": region["contains_critical_minus"],
                        "continues_from_previous_page": region["continues_from_previous_page"],
                        "continues_to_next_page": region["continues_to_next_page"],
                    },
                }
            )
            annotation_id += 1
    return {"info": {"description": "Homework layout silver labels", "version": SCHEMA_VERSION}, "images": images, "annotations": annotations, "categories": categories}


def _validate_coco(coco: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    annotations_by_image: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for annotation in coco["annotations"]:
        annotations_by_image[int(annotation["image_id"])].append(annotation)
        x, y, width, height = annotation["bbox"]
        if min(x, y) < 0 or width <= 0 or height <= 0 or annotation["area"] <= 0:
            failures.append(f"invalid bbox/area annotation {annotation['id']}")
        if len(annotation.get("segmentation", [[]])[0]) != 8:
            failures.append(f"invalid segmentation annotation {annotation['id']}")
    for image in coco["images"]:
        orders = sorted(int(item["read_order"]) for item in annotations_by_image[int(image["id"])])
        if orders != list(range(len(orders))):
            failures.append(f"non-contiguous read_order image {image['id']}")
    return failures


def _create_archive(output_dir: Path) -> tuple[Path, str]:
    archive_path = output_dir.with_suffix(".tar")
    if archive_path.exists():
        archive_path.unlink()
    prefix = output_dir.name
    with tarfile.open(archive_path, "w") as bundle:
        for path in sorted(output_dir.rglob("*")):
            relative = path.relative_to(output_dir)
            if relative.parts and relative.parts[0] == "images_mask":
                continue
            bundle.add(path, arcname=(Path(prefix) / relative).as_posix(), recursive=False)
        for image in sorted((output_dir / "images").glob("*.png")):
            info = tarfile.TarInfo((Path(prefix) / "images_mask" / image.name).as_posix())
            info.type = tarfile.LNKTYPE
            info.linkname = (Path(prefix) / "images" / image.name).as_posix()
            info.mode = 0o644
            bundle.addfile(info)
    return archive_path, _sha256(archive_path)


def build_dataset(
    *,
    labels_path: Path,
    manifest_path: Path,
    images_dir: Path,
    output_dir: Path,
    private_split_path: Path,
    version: str,
    force: bool = False,
    alias_map_path: Path | None = None,
    expected_source_pages: int | None = None,
) -> dict[str, Any]:
    records, cleaning_report = clean_records(labels_path, manifest_path, images_dir)
    source_coverage = None
    if alias_map_path is not None:
        source_coverage = audit_alias_coverage(
            records,
            _jsonl(alias_map_path),
            expected_source_pages=expected_source_pages,
        )
    source_digest = hashlib.sha256(f"{_sha256(labels_path)}:{_sha256(manifest_path)}:{version}".encode()).hexdigest()
    page_splits, split_report = assign_splits(records, seed=source_digest)
    for row in records:
        row["split"] = page_splits[row["page_id"]]

    if output_dir.exists():
        if not force or not (output_dir / DATASET_MARKER).is_file():
            raise FileExistsError(f"refusing to replace unrecognized output directory: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    (output_dir / DATASET_MARKER).write_text(version + "\n", encoding="utf-8")
    for row in records:
        filename = f"{row['page_id']}.png"
        _link_or_copy(row["image_path"], output_dir / "images" / filename)
        _link_or_copy(row["image_path"], output_dir / "images_mask" / filename)

    failures: list[str] = []
    for split, filename in (("train", "instance_train.json"), ("validation", "instance_val.json"), ("hidden_test", "instance_hidden_test.json")):
        coco = _coco_for_split(records, split)
        failures.extend(f"{filename}: {failure}" for failure in _validate_coco(coco))
        _write_json(output_dir / "annotations" / filename, coco)

    public_rows = [
        {
            "page_id": row["page_id"],
            "assignment_id": row["assignment_id"],
            "page": row["page"],
            "split": row["split"],
            "image_ref": f"images/{row['page_id']}.png",
            "image_sha256": row["image_sha256"],
            "region_count": len(row["regions"]),
            "label_status": row["label_status"],
        }
        for row in sorted(records, key=lambda item: item["page_id"])
    ]
    private_rows = [
        {**public, "student_hash": row["student_hash"], "local_source_path": str(row["image_path"].resolve())}
        for public, row in zip(public_rows, sorted(records, key=lambda item: item["page_id"]))
    ]
    _write_jsonl(output_dir / "manifest.jsonl", public_rows)
    _write_jsonl(private_split_path, private_rows)

    report = {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": version,
        "source_digest": source_digest,
        "labeling_version": LABELING_VERSION,
        "consensus_version": CONSENSUS_VERSION,
        "quality_version": QUALITY_VERSION,
        "cleaning": cleaning_report,
        "source_coverage": source_coverage,
        "split": split_report,
        "format": "PaddleX COCOInstSegDataset with read_order",
        "bbox_segmentation_policy": "normalized bbox converted to rectangular polygon",
        "pii_policy": {
            "metadata_contains_raw_student_identity": False,
            "images_may_contain_student_identity": True,
            "identity_upload_authorized": True,
            "private_student_split_manifest_uploaded": False,
        },
        "validation_failures": failures,
        "upload_ready": not failures,
    }
    _write_json(output_dir / "dataset_report.json", report)
    if source_coverage:
        scope_details = (
            f"- Canonical unique pages: {source_coverage['labeled_unique_pages']}; exact aliases removed: "
            f"{source_coverage['exact_alias_pages']}; original source pages covered: {source_coverage['source_pages']}."
        )
        coverage_limitation = (
            "- Every original source page is covered through the private exact-alias map; the alias map itself "
            "is local-only and is not included in this upload package."
        )
    else:
        scope_details = "- No full-source alias coverage proof was supplied for this package."
        coverage_limitation = (
            f"- This package contains {cleaning_report['pages']} canonical pages and must not be described as "
            "covering all 2,287 preprocessed source pages."
        )
    data_card = f"""# Homework Layout Silver Dataset {version}

## Scope

- Pages: {cleaning_report['pages']}; regions: {cleaning_report['regions']}; students: {cleaning_report['students']}.
{scope_details}
- Labels: `{LABELING_VERSION}` / `{CONSENSUS_VERSION}` / `{QUALITY_VERSION}`.
- Splits are frozen before training and grouped by student plus exact/near-duplicate clusters.
- Format: PaddleX `COCOInstSegDataset`; every annotation has zero-based contiguous `read_order`.

## Privacy

Upload metadata contains no name, student number, absolute path, API key, or private grouping identifier. Page pixels may contain student identity; the project configuration records explicit authorization for this layout-labeling purpose. The private split map remains local at `{private_split_path.name}` and must not be uploaded.

## Limitations

- These are multimodal-model silver labels, not human gold labels.
- Source labels are bounding boxes, so instance segmentations are rectangular bbox polygons rather than hand-traced masks.
{coverage_limitation}
- Rare categories are forced into training and are not suitable for standalone validation metrics.
"""
    (output_dir / "DATA_CARD.md").write_text(data_card, encoding="utf-8")

    upload_text_files = [*output_dir.glob("*.json*"), *output_dir.glob("*.md"), *output_dir.glob("annotations/*.json")]
    for path in upload_text_files:
        text = path.read_text(encoding="utf-8")
        if WINDOWS_PATH_RE.search(text) or SECRET_RE.search(text) or "student_hash" in text or "source_path" in text:
            failures.append(f"privacy scan failed: {path.relative_to(output_dir)}")
    report["validation_failures"] = failures
    report["upload_ready"] = not failures
    _write_json(output_dir / "dataset_report.json", report)

    inventory = []
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            inventory.append({"path": path.relative_to(output_dir).as_posix(), "bytes": path.stat().st_size, "sha256": _sha256(path)})
    _write_json(output_dir / "upload_inventory.json", {"schema_version": SCHEMA_VERSION, "files": inventory})
    sums = "".join(f"{row['sha256']}  {row['path']}\n" for row in inventory)
    (output_dir / "SHA256SUMS").write_text(sums, encoding="utf-8")
    if failures:
        raise ValueError("; ".join(failures))
    archive_path, archive_sha256 = _create_archive(output_dir)
    _write_json(output_dir.with_suffix(".tar.sha256.json"), {"archive": archive_path.name, "bytes": archive_path.stat().st_size, "sha256": archive_sha256})
    return {**report, "output_dir": str(output_dir), "archive": str(archive_path), "archive_sha256": archive_sha256}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean, freeze, validate, and package layout labels for cloud training.")
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--images-dir", default=str(DEFAULT_IMAGES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--private-split", default=str(DEFAULT_PRIVATE_SPLIT))
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument(
        "--alias-map",
        default=str(DEFAULT_ALIAS_MAP),
        help="Private full-source exact-alias coverage map; validated but never uploaded.",
    )
    parser.add_argument("--expected-source-pages", type=int, default=DEFAULT_EXPECTED_SOURCE_PAGES)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_dataset(
        labels_path=Path(args.labels).resolve(),
        manifest_path=Path(args.manifest).resolve(),
        images_dir=Path(args.images_dir).resolve(),
        output_dir=Path(args.output_dir).resolve(),
        private_split_path=Path(args.private_split).resolve(),
        version=args.version,
        force=args.force,
        alias_map_path=Path(args.alias_map).resolve() if args.alias_map else None,
        expected_source_pages=args.expected_source_pages or None,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
