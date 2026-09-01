from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any

from evaluation.layout_teacher import CONSENSUS_VERSION, LABELING_VERSION, QUALITY_VERSION, validate_layout
from grading_graph.store import atomic_write_json


SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9._-]{12,}")
WINDOWS_PATH_PATTERN = re.compile(r"[A-Za-z]:\\")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def audit_layout_results(
    manifest_path: Path | str,
    results_dir: Path | str,
    *,
    expected_model: str = "gpt-5.6-sol",
    max_pages: int | None = None,
) -> dict[str, Any]:
    manifest = _read_jsonl(Path(manifest_path))
    if max_pages is not None:
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        manifest = manifest[:max_pages]
    root = Path(results_dir)
    results: list[dict[str, Any]] = []
    missing: list[str] = []
    invalid: list[dict[str, str]] = []
    broken_parents: list[dict[str, str]] = []
    for row in manifest:
        page_id = str(row["page_id"])
        path = root / f"{page_id}.json"
        if not path.is_file():
            missing.append(page_id)
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        try:
            final_layout = validate_layout(result.get("final_layout"))
        except Exception as exc:
            invalid.append({"page_id": page_id, "error": f"{type(exc).__name__}:{str(exc)[:160]}"})
            continue
        region_ids = {str(item["region_id"]) for item in final_layout.get("regions") or []}
        for item in final_layout.get("regions") or []:
            parent = str(item.get("parent_region_id") or "")
            if parent and parent not in region_ids:
                broken_parents.append({"page_id": page_id, "region_id": str(item["region_id"])})
        results.append(result)

    metadata = [
        meta
        for result in results
        for name in ("proposal", "critic", "adjudicator", "repair", "quality_verifier", "quality_tiebreaker")
        if (meta := result.get("teacher", {}).get(name))
    ]
    base_calls_per_page = [
        sum(bool(result.get("teacher", {}).get(name)) for name in ("proposal", "critic", "adjudicator"))
        for result in results
    ]
    calls_per_page = [
        base
        + bool(result.get("teacher", {}).get("repair"))
        + bool(result.get("teacher", {}).get("quality_verifier"))
        + bool(result.get("teacher", {}).get("quality_tiebreaker"))
        for result, base in zip(results, base_calls_per_page)
    ]
    serialized = "\n".join(json.dumps(result, ensure_ascii=False) for result in results)
    high_confidence = sum(result.get("consensus", {}).get("status") == "high_confidence_silver" for result in results)
    adjudicated = sum(result.get("consensus", {}).get("status") == "adjudicated_silver" for result in results)
    wrong_labeling_versions = sum(result.get("teacher", {}).get("labeling_version") != LABELING_VERSION for result in results)
    wrong_consensus_versions = sum(result.get("teacher", {}).get("consensus_version") != CONSENSUS_VERSION for result in results)
    wrong_quality_versions = sum(result.get("teacher", {}).get("quality_version") != QUALITY_VERSION for result in results)
    quality_repair_pages = sum(bool(result.get("consensus", {}).get("quality_repair_applied")) for result in results)
    post_repair_quality_candidate_pages = sum(
        bool(result.get("consensus", {}).get("quality_flags_after_repair")) for result in results
    )
    quality_verifier_pages = sum(
        bool(result.get("consensus", {}).get("quality_verifier_applied")) for result in results
    )
    quality_tiebreaker_pages = sum(
        bool(result.get("consensus", {}).get("quality_tiebreaker_applied")) for result in results
    )
    unresolved_quality_pages = sum(
        bool(result.get("consensus", {}).get("unresolved_quality_flags")) for result in results
    )
    quarantined_pages = sum(not bool(result.get("training_eligible", True)) for result in results)
    nonzero_rotations = sum(int(result.get("final_layout", {}).get("rotation_degrees_clockwise", 0) or 0) != 0 for result in results)
    reported_models = sorted({str(meta.get("reported_model") or "") for meta in metadata})
    report = {
        "schema_version": "1.0",
        "labeling_version": LABELING_VERSION,
        "consensus_version": CONSENSUS_VERSION,
        "requested_pages": len(manifest),
        "completed_pages": len(results),
        "missing_pages": missing,
        "invalid_layouts": invalid,
        "broken_parent_regions": broken_parents,
        "high_confidence_silver": high_confidence,
        "adjudicated_silver": adjudicated,
        "adjudication_rate": round(adjudicated / len(results), 6) if results else 0.0,
        "quality_repair_pages": quality_repair_pages,
        "quality_repair_rate": round(quality_repair_pages / len(results), 6) if results else 0.0,
        "post_repair_quality_candidate_pages": post_repair_quality_candidate_pages,
        "quality_verifier_pages": quality_verifier_pages,
        "quality_tiebreaker_pages": quality_tiebreaker_pages,
        "unresolved_quality_pages": unresolved_quality_pages,
        "quarantined_pages": quarantined_pages,
        "average_base_calls_per_page": round(sum(base_calls_per_page) / len(base_calls_per_page), 6) if base_calls_per_page else 0.0,
        "average_calls_per_page": round(sum(calls_per_page) / len(calls_per_page), 6) if calls_per_page else 0.0,
        "input_tokens": sum(int(meta.get("prompt_tokens", 0) or 0) for meta in metadata),
        "output_tokens": sum(int(meta.get("completion_tokens", 0) or 0) for meta in metadata),
        "reported_models": reported_models,
        "wrong_labeling_versions": wrong_labeling_versions,
        "wrong_consensus_versions": wrong_consensus_versions,
        "wrong_quality_versions": wrong_quality_versions,
        "nonzero_final_rotations": nonzero_rotations,
        "source_path_key_leaks": serialized.count('"source_path"'),
        "windows_path_leaks": len(WINDOWS_PATH_PATTERN.findall(serialized)),
        "secret_hits": len(SECRET_PATTERN.findall(serialized)),
    }
    report["automatic_gate_passed"] = bool(
        report["completed_pages"] == report["requested_pages"]
        and not missing
        and not invalid
        and not broken_parents
        and report["adjudication_rate"] <= 0.55
        and report["quality_repair_rate"] <= 0.12
        and report["average_base_calls_per_page"] <= 2.55
        and report["average_calls_per_page"] <= 2.67
        and reported_models == [expected_model]
        and wrong_labeling_versions == 0
        and wrong_consensus_versions == 0
        and wrong_quality_versions == 0
        and unresolved_quality_pages == 0
        and quarantined_pages == 0
        and nonzero_rotations == 0
        and report["source_path_key_leaks"] == 0
        and report["windows_path_leaks"] == 0
        and report["secret_hits"] == 0
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit layout-teacher outputs before batch labeling.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-model", default="gpt-5.6-sol")
    parser.add_argument("--max-pages", type=int)
    args = parser.parse_args()
    report = audit_layout_results(
        args.manifest,
        args.results_dir,
        expected_model=args.expected_model,
        max_pages=args.max_pages,
    )
    atomic_write_json(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["automatic_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
