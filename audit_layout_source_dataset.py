from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PIL import Image


SECRET_RE = re.compile(r"(?:^|[^A-Za-z])sk-(?:sp-)?[A-Za-z0-9._-]{12,}|Bearer\s+[A-Za-z0-9._-]{20,}")
WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}: line {line_number} must be an object")
        rows.append(value)
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_source_dataset(
    manifest_path: Path,
    private_manifest_path: Path,
    orientation_audit_path: Path,
    *,
    expected_pages: int,
    workers: int = 4,
) -> dict[str, Any]:
    public_text = manifest_path.read_text(encoding="utf-8")
    public_rows = _read_jsonl(manifest_path)
    private_rows = _read_jsonl(private_manifest_path)
    image_root = (manifest_path.parent / "images").resolve()
    failures: list[str] = []
    warnings: list[str] = []

    if len(public_rows) != expected_pages:
        failures.append(f"public page count {len(public_rows)} != {expected_pages}")
    if len(private_rows) != expected_pages:
        failures.append(f"private page count {len(private_rows)} != {expected_pages}")
    public_ids = [str(row.get("page_id") or "") for row in public_rows]
    private_ids = [str(row.get("page_id") or "") for row in private_rows]
    if len(set(public_ids)) != len(public_ids):
        failures.append("duplicate public page_id")
    if len(set(private_ids)) != len(private_ids):
        failures.append("duplicate private page_id")
    if set(public_ids) != set(private_ids):
        failures.append("public/private page_id sets differ")
    if "source_path" in public_text or WINDOWS_PATH_RE.search(public_text):
        failures.append("public manifest leaks a source path")
    if SECRET_RE.search(public_text):
        failures.append("public manifest contains a secret-like value")

    private_by_id = {str(row["page_id"]): row for row in private_rows}

    def inspect(row: dict[str, Any]) -> dict[str, Any]:
        page_id = str(row.get("page_id") or "")
        issues: list[str] = []
        if not re.fullmatch(r"[0-9a-f]{64}", page_id):
            issues.append("invalid page_id")
        relative = Path(str(row.get("rectified_image_ref") or ""))
        image_path = (manifest_path.parent / relative).resolve()
        try:
            image_path.relative_to(image_root)
        except ValueError:
            issues.append("image path escapes image root")
        if image_path.name != f"{page_id}.png":
            issues.append("image filename/page_id mismatch")
        if not image_path.is_file():
            return {"page_id": page_id, "issues": [*issues, "image missing"]}
        actual_hash = _sha256(image_path)
        expected_hash = str(row.get("rectified_sha256") or "")
        if actual_hash != expected_hash:
            issues.append("image sha256 mismatch")
        try:
            with Image.open(image_path) as image:
                width, height = image.size
                image.verify()
            if (width, height) != (int(row.get("rectified_width", 0)), int(row.get("rectified_height", 0))):
                issues.append("image dimensions mismatch")
        except Exception as exc:
            issues.append(f"image decode failed:{type(exc).__name__}")
        private = private_by_id.get(page_id)
        if private is None:
            issues.append("private row missing")
        else:
            private_source = Path(str(private.get("source_path") or "")).resolve()
            if private_source != image_path:
                issues.append("private source/public image mismatch")
            if str(private.get("image_sha256") or "") != actual_hash:
                issues.append("private image sha256 mismatch")
        return {"page_id": page_id, "image_sha256": actual_hash, "issues": issues}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        inspected = list(executor.map(inspect, public_rows))
    failures.extend(
        f"{item['page_id']}:{issue}"
        for item in inspected
        for issue in item["issues"]
    )
    actual_hashes = [str(item.get("image_sha256") or "") for item in inspected if item.get("image_sha256")]
    duplicates = {value: count for value, count in Counter(actual_hashes).items() if count > 1}
    if duplicates:
        warnings.append(f"exact duplicate image hashes: {len(duplicates)}")

    orientation: dict[str, Any] = {}
    if orientation_audit_path.is_file():
        orientation = json.loads(orientation_audit_path.read_text(encoding="utf-8"))
        if int(orientation.get("pages", 0)) != expected_pages:
            failures.append("orientation audit page count mismatch")
        if int(orientation.get("residual_nonzero_pages", 0)) != 0:
            failures.append("orientation audit has residual non-zero pages")
        if int(orientation.get("residual_ambiguous_pages", 0)):
            warnings.append(f"residual orientation-ambiguous pages: {orientation['residual_ambiguous_pages']}")
    else:
        failures.append("orientation audit missing")

    report = {
        "schema_version": "1.0",
        "expected_pages": expected_pages,
        "public_pages": len(public_rows),
        "private_pages": len(private_rows),
        "verified_images": sum(not item["issues"] for item in inspected),
        "assignments": dict(sorted(Counter(str(row.get("assignment_id") or "") for row in public_rows).items())),
        "students": len({str(row.get("student_hash") or "") for row in public_rows}),
        "orientation": {
            "rotated_pages": int(orientation.get("rotated_pages", 0)),
            "preprocess_ambiguous_pages": int(orientation.get("preprocess_ambiguous_pages", 0)),
            "residual_nonzero_pages": int(orientation.get("residual_nonzero_pages", 0)),
            "residual_ambiguous_pages": int(orientation.get("residual_ambiguous_pages", 0)),
            "suppressed_unstable_pages": int(orientation.get("suppressed_unstable_pages", 0)),
        },
        "exact_duplicate_hashes": duplicates,
        "warnings": warnings,
        "failures": failures,
        "ready_for_online_labeling": not failures,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify every preprocessed layout page before online labeling.")
    parser.add_argument("--manifest", default="datasets/layout_all_v3/manifest.jsonl")
    parser.add_argument("--private-manifest", default="runtime_logs/teacher_labeling/all_rectified_v3_private.jsonl")
    parser.add_argument("--orientation-audit", default="datasets/layout_all_v3/qa/orientation_audit.json")
    parser.add_argument("--output", default="datasets/layout_all_v3/preflight_report.json")
    parser.add_argument("--expected-pages", type=int, default=2287)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    report = audit_source_dataset(
        Path(args.manifest).resolve(),
        Path(args.private_manifest).resolve(),
        Path(args.orientation_audit).resolve(),
        expected_pages=args.expected_pages,
        workers=args.workers,
    )
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ready_for_online_labeling"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
