from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.grading_graph.adapters.batch import run_candidate_states
from app.grading_graph.provider import DASHSCOPE_API_KEY_ENV, DashScopeOpenAIProvider


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run prepared LangGraph states in candidate-only mode. Legacy results are never written."
    )
    parser.add_argument("--input-jsonl", required=True, help="JSONL containing one prepared graph state per student")
    parser.add_argument("--artifact-root", required=True, help="Assignment/week directory for agent_artifacts")
    parser.add_argument("--checkpoint-dir", default=None, help="Directory for local SQLite checkpoints")
    parser.add_argument("--cache-dir", default=None, help="Directory for content-addressed provider responses")
    parser.add_argument("--max-students", type=int, default=None, help="Required positive sample count for online runs")
    parser.add_argument("--max-calls", type=int, default=None, help="Per-student maximum model calls")
    parser.add_argument("--max-input-tokens", type=int, default=None, help="Per-student maximum input tokens")
    parser.add_argument("--max-output-tokens", type=int, default=None, help="Per-student maximum output tokens")
    parser.add_argument(
        "--online",
        action="store_true",
        help=f"Explicitly allow DashScope calls using {DASHSCOPE_API_KEY_ENV}; omitted by default",
    )
    return parser.parse_args()


def _read_states(path: Path, max_students: int) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"graph state at line {line_number} must be an object")
            for required in ("run_id", "assignment_id", "student_id", "question_jobs"):
                if not value.get(required):
                    raise ValueError(f"graph state at line {line_number} is missing {required}")
            states.append(value)
            if max_students and len(states) >= max_students:
                break
    return states


def _apply_budget_overrides(states: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    overrides = {
        "max_calls": args.max_calls,
        "max_input_tokens": args.max_input_tokens,
        "max_output_tokens": args.max_output_tokens,
    }
    return [
        {
            **state,
            "budget": {
                **dict(state.get("budget") or {}),
                **{key: value for key, value in overrides.items() if value is not None},
            },
        }
        for state in states
    ]


def main() -> int:
    args = parse_args()
    if not args.online:
        print("Refusing to call DashScope without explicit --online.", file=sys.stderr)
        return 2
    if args.max_students is None or args.max_students <= 0:
        raise SystemExit("--online requires explicit --max-students > 0")
    missing_budget_flags = [
        flag for flag, value in (
            ("--max-calls", args.max_calls),
            ("--max-input-tokens", args.max_input_tokens),
            ("--max-output-tokens", args.max_output_tokens),
        ) if value is None
    ]
    if missing_budget_flags:
        raise SystemExit("--online requires explicit " + ", ".join(missing_budget_flags))
    if args.max_calls <= 0 or args.max_input_tokens <= 0 or args.max_output_tokens <= 0:
        raise SystemExit("online budgets must be greater than zero")

    input_path = Path(args.input_jsonl).resolve()
    artifact_root = Path(args.artifact_root).resolve()
    if not input_path.is_file():
        raise SystemExit(f"input JSONL does not exist: {input_path}")
    states = _apply_budget_overrides(_read_states(input_path, args.max_students), args)
    provider = DashScopeOpenAIProvider.from_environment(max_output_tokens=args.max_output_tokens)
    checkpoint_dir = Path(args.checkpoint_dir).resolve() if args.checkpoint_dir else artifact_root / "agent_artifacts" / "_checkpoints"
    cache_dir = Path(args.cache_dir).resolve() if args.cache_dir else None
    pipeline_config = json.loads(
        (Path(__file__).resolve().parents[2] / "app" / "configs" / "agent_pipeline.json").read_text(encoding="utf-8")
    )

    summary = run_candidate_states(
        provider=provider,
        states=states,
        artifact_root=artifact_root,
        checkpoint_dir=checkpoint_dir,
        cache_dir=cache_dir,
        max_students=args.max_students,
        pipeline_config=pipeline_config,
    )
    print(json.dumps(summary.__dict__, ensure_ascii=False))
    return 1 if summary.failed or summary.stop_reason else 0


if __name__ == "__main__":
    raise SystemExit(main())
