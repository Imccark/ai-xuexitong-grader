from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from app.grading_graph.nodes.ingest import IngestLimits, build_ingest_manifest
from app.grading_graph.nodes.image_quality import WORKING_MAX_PIXELS, analyze_image_bytes
from app.grading_graph.store import atomic_write_json


def _student_hash(student_id: str) -> str:
    return hashlib.sha256(student_id.encode("utf-8")).hexdigest()


def dry_run_raw_submissions(
    root: Path | str,
    *,
    output_path: Path | str | None = None,
    limits: IngestLimits | None = None,
) -> dict[str, Any]:
    """Inspect all raw submission archives without touching processed/results data."""
    root_path = Path(root).resolve()
    archives = sorted(root_path.glob("第*周/raw_submissions/*.zip"))
    records: list[dict[str, Any]] = []
    for archive in archives:
        try:
            manifest = build_ingest_manifest(archive, limits=limits)
            records.append(
                {
                    "assignment_id": archive.parent.parent.name,
                    "student_hash": _student_hash(archive.stem),
                    "source_sha256": manifest["source_sha256"],
                    "status": manifest["status"],
                    "supported_file_count": manifest["supported_file_count"],
                    "file_count": len(manifest["files"]),
                }
            )
        except Exception as exc:
            records.append(
                {
                    "assignment_id": archive.parent.parent.name,
                    "student_hash": _student_hash(archive.stem),
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }
            )
    report = {
        "schema_version": "1.0",
        "mode": "dry_run",
        "source_root": str(root_path),
        "submission_count": len(records),
        "ready_count": sum(1 for item in records if item["status"] == "ready"),
        "unsupported_count": sum(1 for item in records if item["status"] == "unsupported"),
        "failed_count": sum(1 for item in records if item["status"] == "failed"),
        "records": records,
        "mutated_processed_or_results": False,
    }
    if output_path is not None:
        atomic_write_json(Path(output_path), report)
    return report


def dry_run_processed_image_quality(
    root: Path | str,
    *,
    output_path: Path | str | None = None,
) -> dict[str, Any]:
    """Measure existing processed pages read-only, retaining only hashed student IDs."""
    root_path = Path(root).resolve()
    image_paths = sorted(root_path.glob("第*周/processed_images/*/page_*.png"))
    records: list[dict[str, Any]] = []
    for image_path in image_paths:
        assignment_id = image_path.parent.parent.parent.name
        student_id = image_path.parent.name
        page_match = re.search(r"page_(\d+)$", image_path.stem, flags=re.IGNORECASE)
        try:
            profile = analyze_image_bytes(image_path.read_bytes())
            records.append(
                {
                    "assignment_id": assignment_id,
                    "student_hash": _student_hash(student_id),
                    "page": int(page_match.group(1)) if page_match else 0,
                    "width": profile["width"],
                    "height": profile["height"],
                    "working_width": profile.get("working_width", profile["width"]),
                    "working_height": profile.get("working_height", profile["height"]),
                    "working_pixels": int(profile.get("working_width", profile["width"])) * int(profile.get("working_height", profile["height"])),
                    "content_bbox": profile["content_bbox"],
                    "perceptual_hash": profile["perceptual_hash"],
                    "is_near_blank": profile["is_near_blank"],
                    "flags": profile["flags"],
                }
            )
        except Exception as exc:
            records.append(
                {
                    "assignment_id": assignment_id,
                    "student_hash": _student_hash(student_id),
                    "page": int(page_match.group(1)) if page_match else 0,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }
            )
    duplicate_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        fingerprint = record.get("perceptual_hash")
        if fingerprint and record.get("status") != "failed":
            key = (str(record.get("assignment_id")), str(record.get("student_hash")), str(fingerprint))
            duplicate_groups.setdefault(key, []).append(record)
    duplicate_group_count = 0
    duplicate_page_count = 0
    for group in duplicate_groups.values():
        if len(group) < 2:
            continue
        duplicate_group_count += 1
        duplicate_page_count += len(group)
        for record in group:
            record["duplicate_group_size"] = len(group)
            record["is_duplicate_within_student"] = True
    report = {
        "schema_version": "1.0",
        "mode": "processed_image_quality_dry_run",
        "source_root": str(root_path),
        "page_count": len(records),
        "failed_count": sum(1 for item in records if item.get("status") == "failed"),
        "near_blank_count": sum(1 for item in records if item.get("is_near_blank") is True),
        "working_pixel_limit": WORKING_MAX_PIXELS,
        "max_working_pixels": max((int(item.get("working_pixels", 0) or 0) for item in records), default=0),
        "working_copy_bounded": all(
            int(item.get("working_pixels", 0) or 0) <= WORKING_MAX_PIXELS
            for item in records
            if item.get("status") != "failed"
        ),
        "duplicate_group_count": duplicate_group_count,
        "duplicate_page_count": duplicate_page_count,
        "records": records,
        "mutated_processed_or_results": False,
    }
    if output_path is not None:
        atomic_write_json(Path(output_path), report)
    return report
