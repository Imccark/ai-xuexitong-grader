from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from grading_graph.store import atomic_write_json

from .judgment_schema import STUDENT_HASH_RE, VERDICTS, read_jsonl


REQUIRED_PASSES = {"independent", "critic", "adjudicator"}


def validate_model_judgments(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(records)
    judge_rows = [
        row
        for row in rows
        if str(row.get("annotation_source") or "") == "independent_multimodal_model_judge"
    ]
    malformed: list[str] = []
    confirmed = disputed = supported = 0
    unique_keys: set[tuple[str, str, str]] = set()
    duplicate_count = 0
    for index, row in enumerate(judge_rows, 1):
        key = (
            str(row.get("assignment_id") or ""),
            str(row.get("student_hash") or ""),
            str(row.get("question_id") or ""),
        )
        if key in unique_keys:
            duplicate_count += 1
        unique_keys.add(key)
        status = str(row.get("annotation_status") or "")
        passes = row.get("passes")
        reasons: list[str] = []
        if not all(key):
            reasons.append("missing identity")
        if not STUDENT_HASH_RE.fullmatch(key[1]):
            reasons.append("invalid student hash")
        if status not in {"model_confirmed", "model_disputed"}:
            reasons.append("invalid status")
        if not isinstance(passes, dict) or not REQUIRED_PASSES.issubset(passes):
            reasons.append("missing three-pass artifacts")
        if status == "model_confirmed":
            confirmed += 1
            if row.get("scoreable") is not True:
                reasons.append("confirmed row is not scoreable")
            if str(row.get("expected_verdict") or "") not in VERDICTS:
                reasons.append("invalid confirmed verdict")
            if float(row.get("judge_confidence", 0) or 0) < 0.8:
                reasons.append("confirmed confidence below 0.8")
            if str(row.get("expected_verdict")) in {"partial", "incorrect"} and not row.get("evidence_refs"):
                reasons.append("deduction has no evidence")
            supported += int(row.get("candidate_supported") is True)
        else:
            disputed += 1
            if row.get("scoreable") is True or row.get("expected_verdict") is not None:
                reasons.append("disputed row must not be scoreable")
        if reasons:
            malformed.append(f"row {index}: {', '.join(reasons)}")

    failures: list[str] = []
    if not judge_rows:
        failures.append("no independent multimodal model judgments")
    if malformed:
        failures.append(f"malformed judgment rows: {len(malformed)}")
    if duplicate_count:
        failures.append(f"duplicate question judgments: {duplicate_count}")
    ready = bool(judge_rows) and not failures
    return {
        "schema_version": "1.0",
        "status": "passed" if ready else "failed",
        "next_phase_ready": ready,
        "record_count": len(judge_rows),
        "confirmed_count": confirmed,
        "disputed_count": disputed,
        "candidate_supported_count": supported,
        "scoreable_rate": confirmed / len(judge_rows) if judge_rows else None,
        "candidate_support_rate": supported / confirmed if confirmed else None,
        "duplicate_count": duplicate_count,
        "malformed": malformed,
        "failures": failures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate independent multimodal model-judge artifacts.")
    parser.add_argument("--judgments", default="evaluation/model_judgments.jsonl")
    parser.add_argument("--output", default="evaluation/reports/model_judge_gate_report.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = Path(args.judgments).resolve()
    rows = read_jsonl(path) if path.is_file() else []
    report = validate_model_judgments(rows)
    atomic_write_json(Path(args.output).resolve(), report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["next_phase_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
