from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from grading_graph.nodes.image_quality import RECTIFICATION_VERSION, rectify_document_bytes
from grading_graph.store import atomic_write_bytes, atomic_write_json


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "runtime_logs" / "teacher_labeling" / "pilot_private.jsonl"
DEFAULT_OUTPUT = ROOT / "datasets" / "layout_pilot_v4"
DEFAULT_PRIVATE_OUTPUT = ROOT / "runtime_logs" / "teacher_labeling" / "pilot_rectified_v4_private.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    atomic_write_bytes(path, payload.encode("utf-8"))


def prepare_rectified_dataset(
    manifest_path: Path | str,
    output_dir: Path | str,
    private_output_path: Path | str,
    *,
    max_pages: int,
    workers: int = 1,
    orientation_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    all_rows = _read_jsonl(Path(manifest_path))
    manifest = all_rows if max_pages <= 0 else all_rows[:max_pages]
    overrides = orientation_overrides or {}
    manifest_page_ids = {str(row["page_id"]) for row in manifest}
    if unknown_overrides := set(overrides) - manifest_page_ids:
        raise ValueError(f"orientation overrides reference unknown pages: {len(unknown_overrides)}")
    for page_id, override in overrides.items():
        if int(override.get("rotation_degrees_clockwise", 0) or 0) not in (90, 180, 270):
            raise ValueError(f"invalid non-zero orientation override for {page_id}")
    output_root = Path(output_dir).resolve()
    images_root = output_root / "images"
    records_root = output_root / "records"
    images_root.mkdir(parents=True, exist_ok=True)
    records_root.mkdir(parents=True, exist_ok=True)
    public_by_id: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []
    resumed_pages = 0
    started = time.perf_counter()

    def valid_cached_record(row: dict[str, Any]) -> dict[str, Any] | None:
        page_id = str(row["page_id"])
        record_path = records_root / f"{page_id}.json"
        image_path = images_root / f"{page_id}.png"
        if not record_path.is_file() or not image_path.is_file():
            return None
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if record.get("page_id") != page_id or record.get("image_sha256") != row.get("image_sha256"):
                return None
            if record.get("rectification_version") != RECTIFICATION_VERSION:
                return None
            if record.get("orientation_override") != overrides.get(page_id):
                return None
            orientation = (record.get("geometry") or {}).get("orientation") or {}
            if int(record.get("rotation_degrees_clockwise", 0) or 0) and "verification" not in orientation:
                return None
            if hashlib.sha256(image_path.read_bytes()).hexdigest() != record.get("rectified_sha256"):
                return None
            return record
        except Exception as exc:
            return None

    pending: list[dict[str, Any]] = []
    for row in manifest:
        cached = valid_cached_record(row)
        if cached is None:
            pending.append(row)
        else:
            public_by_id[str(row["page_id"])] = cached
            resumed_pages += 1

    def process_row(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        page_id = str(row["page_id"])
        source = Path(str(row["source_path"])).resolve()
        source_bytes = source.read_bytes()
        source_sha = hashlib.sha256(source_bytes).hexdigest()
        if source_sha != row["image_sha256"]:
            raise RuntimeError("source image hash changed")
        override = overrides.get(page_id)
        rectified_bytes, geometry = rectify_document_bytes(source_bytes)
        if override is not None:
            base_rectified_sha = hashlib.sha256(rectified_bytes).hexdigest()
            if base_rectified_sha != str(override.get("input_image_sha256") or ""):
                raise RuntimeError("orientation override input image hash mismatch")
            rectified_bytes, geometry = rectify_document_bytes(
                source_bytes,
                orientation_override_degrees=int(override["rotation_degrees_clockwise"]),
            )
        orientation = geometry.get("orientation") if isinstance(geometry.get("orientation"), dict) else {}
        if not orientation.get("available"):
            raise RuntimeError(f"document orientation unavailable: {orientation.get('reason', 'unknown')}")
        image_path = images_root / f"{page_id}.png"
        atomic_write_bytes(image_path, rectified_bytes)
        rectified_sha = hashlib.sha256(rectified_bytes).hexdigest()
        public = {
            **{key: value for key, value in row.items() if key != "source_path"},
            "rectification_version": geometry["version"],
            "rectification_status": "rectified" if geometry["applied"] else "fallback_normalized",
            "rectified_image_ref": f"images/{page_id}.png",
            "rectified_sha256": rectified_sha,
            "rectified_width": geometry["output_width"],
            "rectified_height": geometry["output_height"],
            "rotation_degrees_clockwise": int(orientation.get("rotation_degrees_clockwise", 0) or 0),
            "orientation_confidence": float(orientation.get("confidence", 0.0) or 0.0),
            "orientation_status": str(orientation.get("reason") or "unknown"),
            "geometry": geometry,
            "orientation_override": override,
        }
        atomic_write_json(records_root / f"{page_id}.json", public)
        return page_id, public

    if workers <= 1:
        iterator = ((row, None) for row in pending)
        for row, _unused in iterator:
            try:
                page_id, public = process_row(row)
                public_by_id[page_id] = public
            except Exception as exc:
                failures.append({"page_id": str(row["page_id"]), "error_type": type(exc).__name__, "message": str(exc)[:300]})
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rectify") as executor:
            futures = {executor.submit(process_row, row): row for row in pending}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    page_id, public = future.result()
                    public_by_id[page_id] = public
                except Exception as exc:
                    failures.append({"page_id": str(row["page_id"]), "error_type": type(exc).__name__, "message": str(exc)[:300]})

    public_rows = [public_by_id[str(row["page_id"])] for row in manifest if str(row["page_id"]) in public_by_id]
    private_rows = [
        {
            **public,
            "source_path": str((images_root / f"{public['page_id']}.png").resolve()),
            "image_sha256": public["rectified_sha256"],
            "original_image_sha256": public["image_sha256"],
            "geometry_preprocessed": True,
        }
        for public in public_rows
    ]
    _write_jsonl(output_root / "manifest.jsonl", public_rows)
    _write_jsonl(Path(private_output_path), private_rows)
    report = {
        "schema_version": "1.0",
        "requested_pages": len(manifest),
        "completed_pages": len(public_rows),
        "rectified_pages": sum(row["rectification_status"] == "rectified" for row in public_rows),
        "fallback_pages": sum(row["rectification_status"] == "fallback_normalized" for row in public_rows),
        "orientation_rotated_pages": sum(int(row["rotation_degrees_clockwise"]) != 0 for row in public_rows),
        "orientation_ambiguous_pages": sum(row["orientation_status"] == "low_orientation_confidence" for row in public_rows),
        "orientation_candidate_search_pages": sum(
            ((row.get("geometry") or {}).get("orientation") or {}).get("candidate_search") is not None
            for row in public_rows
        ),
        "orientation_candidate_search_resolved_pages": sum(
            bool(
                ((((row.get("geometry") or {}).get("orientation") or {}).get("candidate_search") or {}).get("accepted"))
            )
            for row in public_rows
        ),
        "orientation_unstable_pages": sum(row["orientation_status"] == "unstable_orientation_cycle" for row in public_rows),
        "orientation_overrides_requested": len(overrides),
        "orientation_overrides_applied": sum(row.get("orientation_override") is not None for row in public_rows),
        "rotation_counts": {
            str(degrees): sum(int(row["rotation_degrees_clockwise"]) == degrees for row in public_rows)
            for degrees in (0, 90, 180, 270)
        },
        "failed_pages": len(failures),
        "workers": workers,
        "resumed_pages": resumed_pages,
        "newly_processed_pages": len(public_rows) - resumed_pages,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "images_directory": str(images_root),
        "public_manifest": str(output_root / "manifest.jsonl"),
        "private_manifest": str(Path(private_output_path).resolve()),
        "failures": failures,
    }
    atomic_write_json(output_root / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Create geometry-normalized images for the Sol layout-labeling pilot.")
    parser.add_argument("--manifest", default=str(DEFAULT_INPUT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--private-output", default=str(DEFAULT_PRIVATE_OUTPUT))
    parser.add_argument("--max-pages", type=int, default=120)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--orientation-overrides", help="Validated teacher-consensus orientation overrides in JSONL format.")
    args = parser.parse_args()
    if args.workers <= 0:
        raise SystemExit("--workers must be positive")
    override_rows = _read_jsonl(Path(args.orientation_overrides).resolve()) if args.orientation_overrides else []
    orientation_overrides = {str(row.get("page_id")): row for row in override_rows}
    if len(orientation_overrides) != len(override_rows):
        raise SystemExit("orientation override file contains duplicate page_id values")
    report = prepare_rectified_dataset(
        args.manifest,
        args.output_dir,
        args.private_output,
        max_pages=args.max_pages,
        workers=args.workers,
        orientation_overrides=orientation_overrides,
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["failed_pages"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
