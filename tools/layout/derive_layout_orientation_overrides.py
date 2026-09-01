from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from evaluation.layout_teacher import CONSENSUS_VERSION, LABELING_VERSION


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def derive_orientation_overrides(
    manifest_rows: list[dict[str, Any]],
    results_dir: Path,
    *,
    expected_model: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = {str(row.get("page_id")): row for row in manifest_rows}
    if len(manifest) != len(manifest_rows):
        raise ValueError("manifest contains duplicate page_id values")
    overrides: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    inspected_results = 0
    nonzero_results = 0
    for path in sorted(results_dir.glob("*.json")):
        if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
            continue
        inspected_results += 1
        raw = path.read_bytes()
        result = json.loads(raw)
        page_id = str(result.get("page_id") or "")
        source = manifest.get(page_id)
        if source is None:
            unresolved.append({"page_id": page_id, "reason": "result_missing_from_manifest"})
            continue
        final_rotation = int((result.get("final_layout") or {}).get("rotation_degrees_clockwise", 0) or 0)
        if final_rotation == 0:
            continue
        nonzero_results += 1
        if final_rotation not in (90, 180, 270):
            unresolved.append({"page_id": page_id, "reason": "invalid_final_rotation"})
            continue
        if result.get("image_sha256") != source.get("image_sha256"):
            unresolved.append({"page_id": page_id, "reason": "input_image_hash_mismatch"})
            continue
        teacher = result.get("teacher") or {}
        if teacher.get("labeling_version") != LABELING_VERSION or teacher.get("consensus_version") != CONSENSUS_VERSION:
            unresolved.append({"page_id": page_id, "reason": "wrong_teacher_version"})
            continue
        votes: dict[str, int] = {}
        reported_models: set[str] = set()
        for pass_name in ("proposal", "critic", "adjudicator"):
            layout = result.get(pass_name)
            meta = teacher.get(pass_name)
            if layout is None:
                continue
            votes[pass_name] = int(layout.get("rotation_degrees_clockwise", 0) or 0)
            if isinstance(meta, dict):
                reported_models.add(str(meta.get("reported_model") or ""))
        if len(votes) < 2 or any(rotation != final_rotation for rotation in votes.values()):
            unresolved.append({"page_id": page_id, "reason": "independent_rotation_votes_disagree"})
            continue
        if reported_models != {expected_model}:
            unresolved.append({"page_id": page_id, "reason": "reported_model_mismatch"})
            continue
        overrides.append(
            {
                "schema_version": "1.0",
                "page_id": page_id,
                "input_image_sha256": str(result["image_sha256"]),
                "rotation_degrees_clockwise": final_rotation,
                "votes": votes,
                "reported_model": expected_model,
                "labeling_version": LABELING_VERSION,
                "consensus_version": CONSENSUS_VERSION,
                "source_result_sha256": _sha256(raw),
            }
        )
    overrides.sort(key=lambda row: row["page_id"])
    report = {
        "schema_version": "1.0",
        "inspected_results": inspected_results,
        "nonzero_rotation_results": nonzero_results,
        "accepted_overrides": len(overrides),
        "unresolved_nonzero_results": len(unresolved),
        "unresolved": unresolved,
        "automatic_gate_passed": nonzero_results == len(overrides) and not unresolved,
    }
    return overrides, report


def main() -> int:
    parser = argparse.ArgumentParser(description="Derive strict teacher-consensus orientation overrides.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--expected-model", default="gpt-5.6-sol")
    args = parser.parse_args()
    overrides, report = derive_orientation_overrides(
        _read_jsonl(Path(args.manifest).resolve()),
        Path(args.results_dir).resolve(),
        expected_model=args.expected_model,
    )
    _write_jsonl(Path(args.output).resolve(), overrides)
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["automatic_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
