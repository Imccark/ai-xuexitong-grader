from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evaluation.judgment_schema import read_jsonl
from evaluation.metrics import evaluate_candidates
from grading_graph.store import atomic_write_json


def _candidate_files(root: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for path in sorted(root.glob("**/agent_artifacts/*/candidate_result.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            values.append(value)
    return values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute auditable candidate-vs-teacher metrics.")
    parser.add_argument("--gold", help="optional legacy teacher-confirmed reference JSONL")
    parser.add_argument("--model-judgments", help="independent multimodal model judgments JSONL")
    parser.add_argument("--reference-source", choices=["teacher", "model", "hybrid"], default="model")
    parser.add_argument("--candidate-root", required=True, help="repository root containing agent_artifacts")
    parser.add_argument("--symbol-hard-set", help="optional teacher-confirmed symbol_hard_set.jsonl")
    parser.add_argument("--legacy-usage", help="optional JSON object keyed by student_hash with old input/output tokens")
    parser.add_argument("--run-records", help="optional JSONL run records with candidate_available")
    parser.add_argument("--strong-model", action="append", default=None, help="strong-model name; repeat for multiple names")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    gold_path = Path(args.gold).resolve() if args.gold else None
    candidate_root = Path(args.candidate_root).resolve()
    output = Path(args.output).resolve()
    gold = read_jsonl(gold_path) if gold_path and gold_path.is_file() else []
    model_judgments = read_jsonl(Path(args.model_judgments).resolve()) if args.model_judgments else []
    candidates = _candidate_files(candidate_root)
    symbol_path = Path(args.symbol_hard_set).resolve() if args.symbol_hard_set else None
    symbol_records = read_jsonl(symbol_path) if symbol_path and symbol_path.is_file() else None
    legacy_usage = None
    if args.legacy_usage:
        legacy_usage = json.loads(Path(args.legacy_usage).resolve().read_text(encoding="utf-8"))
        if not isinstance(legacy_usage, dict):
            raise ValueError("--legacy-usage must contain a JSON object")
    run_records = read_jsonl(Path(args.run_records).resolve()) if args.run_records else None
    report = evaluate_candidates(
        gold,
        candidates,
        symbol_records=symbol_records,
        legacy_usage=legacy_usage,
        run_records=run_records,
        strong_model_names=args.strong_model,
        model_judgments=model_judgments,
        reference_source=args.reference_source,
    )
    report.update({
        "schema_version": "1.0",
        "gold_path": str(gold_path) if gold_path else None,
        "candidate_root": str(candidate_root),
        "candidate_count": len(candidates),
        "reference_source": args.reference_source,
        "teacher_gold_confirmed": any(item.get("annotation_status") in {"confirmed", "teacher_confirmed"} for item in gold),
        "model_judgment_count": len(model_judgments),
    })
    atomic_write_json(output, report)
    print(json.dumps({"gold_records": len(gold), "candidate_count": len(candidates)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
