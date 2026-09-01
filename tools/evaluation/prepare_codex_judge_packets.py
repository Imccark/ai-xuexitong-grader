from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.evaluation.core.codex_judge_packets import prepare_packets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare anonymized blind-then-reveal packets for Codex judging.")
    parser.add_argument("--candidate-root", default=".")
    parser.add_argument("--manifest-root", default="evaluation/answer_manifests")
    parser.add_argument("--output-root", default="evaluation/codex_judge_packets")
    parser.add_argument("--max-students", type=int, required=True)
    parser.add_argument("--max-questions", type=int, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = prepare_packets(
        root=Path(args.candidate_root),
        manifest_root=Path(args.manifest_root),
        output_root=Path(args.output_root),
        max_students=args.max_students,
        max_questions=args.max_questions,
        overwrite=args.overwrite,
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["packet_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
