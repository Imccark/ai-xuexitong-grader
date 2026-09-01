from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

QUALITY_THRESHOLDS = {
    "question_verdict_accuracy": 0.95,
    "overall_accuracy": 0.96,
    "question_coverage_recall": 0.99,
    "negative_sign_recall": 0.99,
    "critical_symbol_precision": 0.98,
    "error_accusation_false_positive_rate": 0.02,
    "severe_misjudgment_rate": 0.005,
    "average_token_ratio": 0.60,
    "p95_token_ratio": 0.70,
    "strong_model_trigger_rate": 0.25,
    "graph_failure_rate": 0.005,
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _metric_pass(name: str, metric: Any) -> tuple[bool, str]:
    if not isinstance(metric, dict) or metric.get("status") != "measured":
        return False, "unmeasured"
    value = metric.get("value")
    if not isinstance(value, (int, float)):
        return False, "missing_value"
    threshold = QUALITY_THRESHOLDS[name]
    passed = value <= threshold if name in {
        "error_accusation_false_positive_rate",
        "severe_misjudgment_rate",
        "average_token_ratio",
        "p95_token_ratio",
        "strong_model_trigger_rate",
        "graph_failure_rate",
    } else value >= threshold
    return passed, f"{value} {'<=' if name in {'error_accusation_false_positive_rate','severe_misjudgment_rate','average_token_ratio','p95_token_ratio','strong_model_trigger_rate','graph_failure_rate'} else '>='} {threshold}"


def audit(
    *,
    report_path: Path,
    metrics_path: Path,
    gate_path: Path,
    packet_index: Path,
    judgments_path: Path,
) -> dict[str, Any]:
    report = _read_json(report_path)
    metrics = _read_json(metrics_path)
    gate = _read_json(gate_path)
    packet_rows = [
        json.loads(line)
        for line in packet_index.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    judgment_rows = [
        json.loads(line)
        for line in judgments_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if judgments_path.is_file() else []
    packet_hashes: dict[tuple[str, str, str], str] = {}
    repo_root = packet_index.resolve().parents[2]
    for row in packet_rows:
        candidate_path = repo_root / Path(str(row.get("candidate_context", "")))
        if not candidate_path.is_file():
            continue
        candidate_payload = _read_json(candidate_path)
        candidate = candidate_payload.get("candidate") if isinstance(candidate_payload.get("candidate"), dict) else {}
        snapshot = candidate_payload.get("candidate_snapshot_hash") or candidate.get("candidate_snapshot_hash")
        if snapshot:
            packet_hashes[(str(row.get("assignment_id")), str(row.get("student_hash")), str(row.get("question_id")))] = str(snapshot)
    stale_judgments = 0
    for row in judgment_rows:
        key = (str(row.get("assignment_id")), str(row.get("student_hash")), str(row.get("question_id")))
        expected_hash = packet_hashes.get(key)
        observed_hash = str(row.get("candidate_snapshot_hash") or "")
        if not expected_hash or observed_hash != expected_hash:
            stale_judgments += 1
    checks: dict[str, dict[str, Any]] = {}
    run = report.get("run") or {}
    checks["candidate_run"] = {
        "passed": run.get("processed") == run.get("succeeded") and run.get("failed") == 0 and run.get("stop_reason") is None,
        "observed": {key: run.get(key) for key in ("processed", "succeeded", "failed", "stop_reason")},
    }
    checks["structured_errors"] = {
        "passed": all(value == 0 for value in (report.get("error_breakdown") or {}).values()),
        "observed": report.get("error_breakdown") or {},
    }
    checks["codex_packet_anonymity"] = {
        "passed": report.get("verification", {}).get("codex_judge_packets", "").find("contains_student_names=false") >= 0,
        "observed": {"packet_count": len(packet_rows), "contains_student_names": report.get("verification", {}).get("codex_judge_packets")},
    }
    checks["three_pass_judge_coverage"] = {
        "passed": gate.get("status") == "passed" and gate.get("disputed_count", 0) == 0,
        "observed": {key: gate.get(key) for key in ("record_count", "confirmed_count", "disputed_count")},
    }
    checks["current_judge_snapshot"] = {
        "passed": stale_judgments == 0,
        "observed": {"judgment_rows": len(judgment_rows), "stale_or_unbound": stale_judgments},
    }
    checks["development_judge_sample"] = {
        "passed": int(gate.get("confirmed_count", 0) or 0) >= 30,
        "observed": {"confirmed_count": gate.get("confirmed_count", 0), "required": 30},
    }
    for name in QUALITY_THRESHOLDS:
        if name == "graph_failure_rate" and isinstance(run.get("processed"), int) and run.get("processed", 0) > 0:
            failures = int(run.get("failed", 0) or 0)
            value = failures / int(run["processed"])
            passed = value <= QUALITY_THRESHOLDS[name]
            detail = f"{failures}/{run['processed']} = {value} <= {QUALITY_THRESHOLDS[name]}"
        else:
            passed, detail = _metric_pass(name, metrics.get(name))
        checks[name] = {"passed": passed, "observed": detail}

    failed = [name for name, value in checks.items() if not value["passed"]]
    return {
        "schema_version": "1.0",
        "status": "passed" if not failed else "failed",
        "checks": checks,
        "failed_checks": failed,
        "next_step": "全部检查通过，可进入下一阶段" if not failed else "补齐失败项后重新运行本审计",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the auditable multi-agent grading acceptance gate.")
    parser.add_argument("--report", default="evaluation/reports/qwen_candidate_eval_第一周_20260827.json")
    parser.add_argument("--metrics", default="evaluation/reports/model_judge_metrics.json")
    parser.add_argument("--judge-gate", default="evaluation/reports/model_judge_gate_report.json")
    parser.add_argument("--packet-index", default="evaluation/codex_judge_packets/index.jsonl")
    parser.add_argument("--judgments", default="evaluation/model_judgments.jsonl")
    parser.add_argument("--output", default="evaluation/reports/acceptance_audit.json")
    args = parser.parse_args()
    result = audit(
        report_path=Path(args.report),
        metrics_path=Path(args.metrics),
        gate_path=Path(args.judge_gate),
        packet_index=Path(args.packet_index),
        judgments_path=Path(args.judgments),
    )
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
