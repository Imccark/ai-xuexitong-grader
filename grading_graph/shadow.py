from __future__ import annotations

import json
import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from grading_graph.schemas import CandidateResult, OverallLabel
from grading_graph.store import atomic_write_json, file_sha256


LEGACY_TO_OVERALL = {
    "全对": OverallLabel.ALL_CORRECT.value,
    "全正确": OverallLabel.ALL_CORRECT.value,
    "部分错误": OverallLabel.PARTIAL.value,
    "错误较多": OverallLabel.MANY_ERRORS.value,
    "图片缺失，需人工复核": OverallLabel.UNREADABLE.value,
    "题目版本不匹配，需人工复核": OverallLabel.MISMATCH.value,
}


def _legacy_overall(value: Any) -> str:
    text = str(value or "").strip()
    return LEGACY_TO_OVERALL.get(text, text or OverallLabel.UNKNOWN.value)


def _source_hashes(paths: Iterable[Path]) -> dict[str, str]:
    return {str(path.resolve()): file_sha256(path) for path in sorted(paths) if path.is_file()}


def compare_candidate_to_legacy(candidate: CandidateResult, legacy_payload: dict[str, Any]) -> dict[str, Any]:
    candidate_overall = candidate.overall.value
    legacy_overall = _legacy_overall(legacy_payload.get("overall"))
    return {
        "student_hash": hashlib.sha256(candidate.student_id.encode("utf-8")).hexdigest(),
        "candidate_overall": candidate_overall,
        "legacy_overall": legacy_overall,
        "overall_match": candidate_overall == legacy_overall,
        "candidate_status": candidate.status.value,
        "unresolved_risk_count": candidate.unresolved_risk_count,
        "question_count": len(candidate.question_results),
    }


def run_shadow(
    *,
    candidates: Iterable[CandidateResult],
    legacy_payloads: dict[str, dict[str, Any]],
    formal_result_paths: Iterable[Path] = (),
    report_path: Path | str,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a candidate-only comparison report without touching formal results."""
    paths = [Path(path) for path in formal_result_paths]
    before = _source_hashes(paths)
    differences = [
        compare_candidate_to_legacy(candidate, legacy_payloads.get(candidate.student_id, {}))
        for candidate in candidates
    ]
    after = _source_hashes(paths)
    report = {
        "schema_version": "1.0",
        "mode": "legacy_shadow",
        "formal_result_source": "legacy",
        "candidate_only": True,
        "total_candidates": len(differences),
        "overall_matches": sum(1 for item in differences if item["overall_match"]),
        "differences": differences,
        "formal_result_hashes_before": before,
        "formal_result_hashes_after": after,
        "formal_results_unchanged": before == after,
        "metrics": metrics or {"online_calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0, "elapsed_seconds": 0},
    }
    atomic_write_json(Path(report_path), report)
    return report


def load_json_payload(path: Path | str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value
