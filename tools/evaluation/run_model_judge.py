from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from evaluation.model_judge import MultimodalModelJudge, candidate_snapshot_hash
from grading_graph.budget import BudgetLedger, BudgetedJsonProvider
from grading_graph.cache import CachedJsonProvider, JsonResponseCache
from grading_graph.provider import OpenAIResponsesProvider
from grading_graph.store import atomic_write_bytes
from project_config import read_local_env


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use an independent multimodal model to judge Qwen candidate artifacts."
    )
    parser.add_argument("--candidate-root", default=".")
    parser.add_argument("--manifest-root", default="evaluation/answer_manifests")
    parser.add_argument("--output", default="evaluation/model_judgments.jsonl")
    parser.add_argument("--cache-dir", default="evaluation/model_judge_cache")
    parser.add_argument("--model", default="gpt-5.6")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--reasoning-effort", default="high", choices=["low", "medium", "high", "xhigh"])
    parser.add_argument("--confidence-threshold", type=float, default=0.8)
    parser.add_argument("--max-students", type=int, required=True)
    parser.add_argument("--max-questions", type=int, required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--max-input-tokens", type=int, required=True)
    parser.add_argument("--max-output-tokens", type=int, required=True)
    parser.add_argument("--max-output-tokens-per-call", type=int, default=2048)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--online", action="store_true")
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} must be an object")
        rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    data = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode("utf-8")
    atomic_write_bytes(path, data)


def _candidate_paths(root: Path) -> list[Path]:
    return sorted(root.glob("**/agent_artifacts/*/candidate_result.json"))


def _manifest(manifest_root: Path, assignment_id: str) -> dict[str, Any]:
    path = manifest_root / assignment_id / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"answer manifest missing: {path}")
    return _read_json(path)


def _evidence_pages(result: dict[str, Any]) -> list[int]:
    pages: list[int] = []
    for ref in result.get("evidence_refs") or []:
        if isinstance(ref, dict) and int(ref.get("page", 0) or 0) >= 1:
            pages.append(int(ref["page"]))
    for span in result.get("transcription") or []:
        if isinstance(span, dict) and int(span.get("page", 0) or 0) >= 1:
            pages.append(int(span["page"]))
    return list(dict.fromkeys(pages))[:2]


def _page_images(candidate_path: Path, result: dict[str, Any]) -> list[Path]:
    pages_root = candidate_path.parent / "pages"
    images: list[Path] = []
    for page in _evidence_pages(result):
        page_root = pages_root / f"page_{page}"
        selected = next(
            (page_root / name for name in ("enhanced.png", "normalized.png", "original.png") if (page_root / name).is_file()),
            None,
        )
        if selected is not None:
            images.append(selected)
    return images


def _configure_key(api_key_env: str) -> None:
    if os.environ.get(api_key_env, "").strip():
        return
    local_value = read_local_env().get(api_key_env, "").strip()
    if local_value:
        os.environ[api_key_env] = local_value


def main() -> int:
    args = parse_args()
    if not args.online:
        print("Refusing model-judge calls without explicit --online.", file=sys.stderr)
        return 2
    for name in ("max_students", "max_questions", "max_calls", "max_input_tokens", "max_output_tokens"):
        if int(getattr(args, name)) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be greater than zero")
    if args.max_output_tokens_per_call <= 0:
        raise SystemExit("--max-output-tokens-per-call must be greater than zero")
    if args.max_calls < args.max_questions * 3:
        raise SystemExit("--max-calls must allow three judge passes per question")

    _configure_key(args.api_key_env)
    provider = OpenAIResponsesProvider.from_environment(
        model=args.model,
        api_key_env=args.api_key_env,
        max_output_tokens=min(args.max_output_tokens_per_call, args.max_output_tokens),
        reasoning_effort=args.reasoning_effort,
    )
    ledger = BudgetLedger(
        {
            "max_calls": args.max_calls,
            "max_input_tokens": args.max_input_tokens,
            "max_output_tokens": args.max_output_tokens,
        }
    )
    cached = CachedJsonProvider(provider, JsonResponseCache(Path(args.cache_dir).resolve(), preprocess_version="model-judge-v1"))
    judge = MultimodalModelJudge(
        BudgetedJsonProvider(cached, ledger),
        confidence_threshold=args.confidence_threshold,
    )

    root = Path(args.candidate_root).resolve()
    manifest_root = Path(args.manifest_root).resolve()
    output = Path(args.output).resolve()
    rows = [] if args.overwrite else _read_jsonl(output)
    completed = {
        (str(row.get("assignment_id")), str(row.get("student_hash")), str(row.get("question_id")))
        for row in rows
    }
    judged_questions = 0
    judged_students: set[tuple[str, str]] = set()
    manifest_cache: dict[str, dict[str, Any]] = {}
    for candidate_path in _candidate_paths(root):
        candidate = _read_json(candidate_path)
        assignment_id = str(candidate.get("assignment_id") or "")
        student_hash = candidate_path.parent.name
        student_key = (assignment_id, student_hash)
        if student_key not in judged_students and len(judged_students) >= args.max_students:
            continue
        manifest_cache.setdefault(assignment_id, _manifest(manifest_root, assignment_id))
        references = manifest_cache[assignment_id].get("questions") or {}
        for question_id, raw_result in (candidate.get("question_results") or {}).items():
            if judged_questions >= args.max_questions:
                break
            key = (assignment_id, student_hash, str(question_id))
            if key in completed or not isinstance(raw_result, dict):
                continue
            reference = references.get(str(question_id))
            if not isinstance(reference, dict):
                continue
            row = judge.evaluate_question(
                assignment_id=assignment_id,
                student_hash=student_hash,
                question_id=str(question_id),
                candidate_result=raw_result,
                reference=reference,
                image_refs=_page_images(candidate_path, raw_result),
                candidate_snapshot=candidate_snapshot_hash(raw_result),
            )
            rows.append(row)
            _write_jsonl(output, rows)
            completed.add(key)
            judged_students.add(student_key)
            judged_questions += 1
        if judged_questions >= args.max_questions:
            break

    snapshot = ledger.snapshot
    summary = {
        "judged_students": len(judged_students),
        "judged_questions": judged_questions,
        "scoreable_questions": sum(1 for row in rows if row.get("scoreable") is True),
        "model": args.model,
        "calls": snapshot.calls,
        "estimated_input_tokens": snapshot.input_tokens,
        "estimated_output_tokens": snapshot.output_tokens,
        "output": str(output),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
