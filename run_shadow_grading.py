from __future__ import annotations

import argparse
import json
from pathlib import Path

from grading_graph.schemas import CandidateResult
from grading_graph.shadow import load_json_payload, run_shadow
from project_config import load_runtime_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成新 Graph 的 candidate-only 影子对照报告。")
    parser.add_argument("--assignment", required=True, help="assignment 配置路径")
    parser.add_argument("--report", required=True, help="影子报告输出路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_runtime_config(assignment=args.assignment)
    week_dir = config.week_dir.resolve()
    candidate_paths = sorted((week_dir / "agent_artifacts").glob("*/candidate_result.json"))
    candidates = [CandidateResult.model_validate(load_json_payload(path)) for path in candidate_paths]

    legacy_payloads: dict[str, dict] = {}
    formal_paths = sorted(config.results_dir.glob("*.json")) + sorted(config.results_dir.glob("*.txt"))
    for path in sorted(config.results_dir.glob("*.json")):
        try:
            payload = load_json_payload(path)
        except (OSError, ValueError):
            continue
        student_id = str(payload.get("student_name_or_id") or path.stem)
        legacy_payloads[student_id] = payload

    report = run_shadow(
        candidates=candidates,
        legacy_payloads=legacy_payloads,
        formal_result_paths=formal_paths,
        report_path=Path(args.report),
    )
    print(json.dumps({"candidates": report["total_candidates"], "formal_results_unchanged": report["formal_results_unchanged"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
