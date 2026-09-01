from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from grading_graph.budget import BudgetLedger, RateLimitedJsonProvider
from grading_graph.cache import JsonResponseCache
from grading_graph.graph import GraphExecutionSettings, build_grading_graph
from grading_graph.schemas import (
    AnswerManifest,
    Budget,
    CandidateResult,
    EvidenceRef,
    QuestionJob,
    QuestionResult,
)
from grading_graph.store import atomic_write_json
from grading_graph.nodes.aggregator import aggregate_overall


class TargetedRerunError(RuntimeError):
    """A targeted graph run failed; the previous candidate must remain active."""

    def __init__(self, error_type: str) -> None:
        self.error_type = str(error_type or "TargetedRerunError")
        super().__init__("targeted rerun failed")


def _student_hash(student_id: str) -> str:
    return hashlib.sha256(str(student_id).encode("utf-8")).hexdigest()


def _page_variant(week_dir: Path, student_id: str, page: int) -> Path | None:
    artifact_page = week_dir / "agent_artifacts" / _student_hash(student_id) / "pages" / f"page_{page}"
    for name in ("normalized.png", "original.png"):
        path = artifact_page / name
        if path.is_file():
            return path.resolve()
    processed = week_dir / "processed_images" / student_id / f"page_{page}.png"
    return processed.resolve() if processed.is_file() else None


def _safe_answer_text(manifest: AnswerManifest, manifest_path: Path, question_id: str) -> str:
    answer_slice = manifest.questions.get(question_id)
    if answer_slice is None:
        return ""
    root = manifest_path.resolve().parent
    path = (root / answer_slice.artifact_ref).resolve()
    if root not in path.parents or not path.is_file():
        raise FileNotFoundError(f"answer slice is unavailable for {question_id}")
    return path.read_text(encoding="utf-8")


def _load_saved_job(week_dir: Path, student_id: str, question_id: str) -> QuestionJob | None:
    path = week_dir / "agent_artifacts" / _student_hash(student_id) / "input_manifest.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        raw_job = (value.get("question_jobs") or {}).get(question_id)
        return QuestionJob.model_validate(raw_job) if isinstance(raw_job, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _load_saved_pages(week_dir: Path, student_id: str) -> list[dict[str, Any]]:
    path = week_dir / "agent_artifacts" / _student_hash(student_id) / "input_manifest.json"
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        pages = value.get("pages") if isinstance(value, dict) else None
        return [dict(page) for page in pages if isinstance(page, dict)] if isinstance(pages, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _rebuild_job(
    *,
    week_dir: Path,
    student_id: str,
    question_id: str,
    result: QuestionResult,
    manifest: AnswerManifest,
) -> QuestionJob:
    saved = _load_saved_job(week_dir, student_id, question_id)
    raw_refs = list(saved.roi_refs) if saved is not None else list(result.evidence_refs)
    refs: list[EvidenceRef] = []
    for index, ref in enumerate(raw_refs, 1):
        page_path = _page_variant(week_dir, student_id, ref.page)
        refs.append(
            ref.model_copy(
                update={
                    "span_id": ref.span_id or f"rerun-p{ref.page}-q{index}",
                    "artifact_ref": str(page_path) if page_path is not None else ref.artifact_ref,
                    "view": "normalized" if page_path is not None else ref.view,
                }
            )
        )
    answer_slice = manifest.questions.get(question_id)
    if saved is not None:
        route = saved.route
        question_type = saved.question_type
        pages = saved.pages
    else:
        route = "unreadable" if result.verdict.value == "unreadable" else (
            "risk" if result.needs_verification or result.risk_level.value != "low" else "fast"
        )
        question_type = answer_slice.question_type if answer_slice is not None else "unknown"
        pages = sorted({ref.page for ref in refs})
    return QuestionJob(
        question_id=question_id,
        pages=pages,
        roi_refs=refs,
        answer_slice=answer_slice,
        question_type=question_type,
        route=route,
    )


def build_targeted_rerun_input(
    *,
    week_dir: Path | str,
    candidate: CandidateResult,
    question_id: str,
    answer_manifest_path: Path | str,
    run_id: str,
    budget: Budget,
) -> dict[str, Any]:
    week_dir = Path(week_dir).resolve()
    manifest_path = Path(answer_manifest_path).resolve()
    manifest = AnswerManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    current = candidate.question_results.get(question_id)
    if current is None:
        raise ValueError(f"question does not exist: {question_id}")
    job = _rebuild_job(
        week_dir=week_dir,
        student_id=candidate.student_id,
        question_id=question_id,
        result=current,
        manifest=manifest,
    )
    return {
        "schema_version": "1.0",
        "graph_version": candidate.graph_version,
        "run_id": run_id,
        "assignment_id": candidate.assignment_id,
        "student_id": candidate.student_id,
        "answer_manifest": manifest.model_dump(mode="json"),
        "pages": _load_saved_pages(week_dir, candidate.student_id),
        "question_jobs": {question_id: job.model_dump(mode="json")},
        "transcriptions": {question_id: [span.model_dump(mode="json") for span in current.transcription]},
        "answer_texts": {question_id: _safe_answer_text(manifest, manifest_path, question_id)},
        "budget": budget.model_dump(mode="json"),
    }


def run_targeted_question_rerun(
    *,
    provider: Any,
    week_dir: Path | str,
    candidate: CandidateResult,
    question_id: str,
    answer_manifest_path: Path | str,
    run_id: str,
    budget: Budget,
    checkpoint_path: Path | str | None = None,
    cache_dir: Path | str | None = None,
    pipeline_config: dict[str, Any] | None = None,
) -> CandidateResult:
    """Rerun exactly one question and merge it into a new candidate version."""
    state = build_targeted_rerun_input(
        week_dir=week_dir,
        candidate=candidate,
        question_id=question_id,
        answer_manifest_path=answer_manifest_path,
        run_id=run_id,
        budget=budget,
    )
    ledger = BudgetLedger(budget.model_dump(mode="json"))
    cache = JsonResponseCache(cache_dir) if cache_dir is not None else None
    limited_provider = provider if isinstance(provider, RateLimitedJsonProvider) else RateLimitedJsonProvider(provider)
    if checkpoint_path is None:
        app = build_grading_graph(
            limited_provider,
            cache=cache,
            budget_ledger=ledger,
            execution_settings=GraphExecutionSettings.from_pipeline_config(pipeline_config),
        )
        output = app.invoke(state)
    else:
        from grading_graph.checkpoint import open_sqlite_checkpointer

        with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
            app = build_grading_graph(
                limited_provider,
                checkpointer=checkpointer,
                cache=cache,
                budget_ledger=ledger,
                execution_settings=GraphExecutionSettings.from_pipeline_config(pipeline_config),
            )
            output = app.invoke(state, config={"configurable": {"thread_id": run_id}})
    targeted = CandidateResult.model_validate(output["candidate"])
    targeted_errors = [
        error for error in targeted.errors
        if isinstance(error, dict) and str(error.get("question_id", "")) == question_id
    ]
    if targeted_errors:
        raise TargetedRerunError(str(targeted_errors[0].get("error_type") or "TargetedRerunError"))
    replacement = targeted.question_results.get(question_id)
    if replacement is None:
        raise ValueError(f"targeted graph returned no result for {question_id}")

    question_results = dict(candidate.question_results)
    question_results[question_id] = replacement
    errors = [
        error for error in candidate.errors
        if not isinstance(error, dict) or str(error.get("question_id", "")) != question_id
    ]
    errors.extend(targeted.errors)
    overall, status, unresolved = aggregate_overall(question_results.values())
    if errors and status.value != "reference_mismatch":
        from grading_graph.schemas import StudentStatus

        status = StudentStatus.REVIEW_REQUIRED
    unresolved += len(errors)
    return CandidateResult.model_validate(
        {
            **candidate.model_dump(mode="json"),
            "run_id": run_id,
            "question_results": {key: value.model_dump(mode="json") for key, value in question_results.items()},
            "overall": overall.value,
            "status": status.value,
            "unresolved_risk_count": unresolved,
            "errors": errors,
            "budget_usage": targeted.budget_usage,
        }
    )


def persist_targeted_rerun_audit(
    *,
    week_dir: Path | str,
    student_id: str,
    question_id: str,
    old_run_id: str,
    candidate: CandidateResult,
    provider: Any,
) -> None:
    root = Path(week_dir).resolve() / "agent_artifacts" / _student_hash(student_id)
    atomic_write_json(root / "rerun_audit.json", {
        "schema_version": "1.0",
        "student_hash": _student_hash(student_id),
        "question_id": question_id,
        "old_run_id": old_run_id,
        "new_run_id": candidate.run_id,
        "provider": provider.__class__.__name__,
        "budget_usage": candidate.budget_usage,
        "errors": candidate.errors,
        "langsmith_enabled": False,
    })
