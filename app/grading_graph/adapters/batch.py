from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
import hashlib
import time
from typing import Any, Iterable

from app.grading_graph.graph import GraphExecutionSettings, build_grading_graph, build_image_grading_graph
from app.grading_graph.nodes.image_quality import RECTIFICATION_VERSION
from app.grading_graph.review import ReviewStore
from app.grading_graph.schemas import Budget, CandidateResult
from app.grading_graph.store import atomic_write_json, canonical_hash, canonical_json
from app.grading_graph.state import graph_state_payload, migrate_graph_state


@dataclass(frozen=True)
class CandidateBatchSummary:
    processed: int
    succeeded: int
    failed: int
    failures: tuple[dict[str, str], ...] = ()
    stop_reason: str | None = None


_PROVIDER_ERROR_TYPES = {
    "ConnectionError",
    "TimeoutError",
    "GradingProviderError",
    "TranscriptionProviderError",
    "PageObservationError",
}


def _is_provider_error_type(error_type: Any) -> bool:
    name = str(error_type or "")
    return name in _PROVIDER_ERROR_TYPES or name.endswith("ProviderError")


def candidate_has_provider_error(candidate: CandidateResult) -> bool:
    # A single recovered question must not stop an otherwise healthy batch.
    # Stop protection is reserved for a student whose entire question set is
    # unreadable because of provider failures (or an explicit pipeline
    # failure), which is evidence that the upstream service is unavailable.
    if candidate.status.value == "pipeline_failed":
        return True
    provider_errors = [
        item for item in candidate.errors
        if isinstance(item, dict) and _is_provider_error_type(item.get("error_type"))
    ]
    if not provider_errors:
        return False
    results = list(candidate.question_results.values())
    failed_question_ids = {
        str(item.get("question_id"))
        for item in provider_errors
        if isinstance(item, dict) and item.get("question_id")
    }
    # A previous-candidate recovery can keep labels readable even while every
    # fresh grading request is failing. Treat complete provider-error coverage
    # as an outage so the outer batch stops after its bounded consecutive
    # failure threshold instead of silently replaying an entire old batch.
    all_questions_failed_fresh = bool(results) and set(candidate.question_results).issubset(failed_question_ids)
    return bool(results) and (
        all(result.verdict.value == "unreadable" for result in results)
        or all_questions_failed_fresh
    )


def _recover_provider_questions(
    candidate: CandidateResult,
    previous: CandidateResult | None,
) -> CandidateResult:
    """Reuse a prior same-image question result after a transient provider error.

    Regrades intentionally use a fresh run id, but the processed images and
    answer manifest are unchanged.  If one question exhausts its provider
    retries while a prior candidate has clear evidence for that same question,
    retaining the prior result is safer than replacing it with an unreadable
    placeholder.  The recovery is explicit in the persisted error metadata and
    does not affect questions that produced a fresh result.
    """
    if previous is None or previous.assignment_id != candidate.assignment_id:
        return candidate
    failed_by_question = {
        str(item.get("question_id"))
        for item in candidate.errors
        if isinstance(item, dict)
        and item.get("question_id")
        and _is_provider_error_type(item.get("error_type"))
    }
    if not failed_by_question:
        return candidate
    merged = dict(candidate.question_results)
    recovered: set[str] = set()
    for question_id in failed_by_question:
        current = merged.get(question_id)
        prior = previous.question_results.get(question_id)
        if current is None or prior is None:
            continue
        if current.verdict.value != "unreadable":
            continue
        if prior.verdict.value == "unreadable" or not prior.evidence_refs:
            continue
        merged[question_id] = prior
        recovered.add(question_id)
    if not recovered:
        return candidate
    # Preserve a bounded, non-sensitive audit trail while preventing recovered
    # transient errors from being counted as active provider failures by the
    # batch stop guard.
    errors: list[dict[str, Any]] = []
    for item in candidate.errors:
        question_id = str(item.get("question_id") or "") if isinstance(item, dict) else ""
        if question_id in recovered and isinstance(item, dict):
            errors.append({
                "stage": "recovery",
                "question_id": question_id,
                "error_type": "RecoveredProviderError",
                "original_error_type": str(item.get("error_type") or "ProviderError"),
            })
        else:
            errors.append(item)
    return candidate.model_copy(update={"question_results": merged, "errors": errors})


def _pipeline_artifact_dir(artifact_root: Path | str, student_id: str) -> Path:
    import hashlib

    return Path(artifact_root).resolve() / "agent_artifacts" / hashlib.sha256(student_id.encode("utf-8")).hexdigest()


def _persist_pipeline_artifacts(
    *,
    artifact_root: Path | str,
    student_id: str,
    graph_input: dict[str, Any],
    candidate: CandidateResult,
    provider: Any,
    elapsed_ms: float,
    cache: Any = None,
) -> None:
    """Persist auditable sidecars without replacing the candidate or formal result."""
    graph_input = _serializable(graph_input)
    root = _pipeline_artifact_dir(artifact_root, student_id)
    pages = graph_input.get("pages", [])
    observations = graph_input.get("page_observations", [])
    answer_manifest = graph_input.get("answer_manifest") or {}
    atomic_write_json(root / "input_manifest.json", {
        "schema_version": "1.0",
        "assignment_id": graph_input.get("assignment_id"),
        "student_hash": root.name,
        "run_id": graph_input.get("run_id"),
        "preprocess_version": graph_input.get("preprocess_version", RECTIFICATION_VERSION),
        "pages": pages,
        "answer_manifest": answer_manifest,
        "question_jobs": graph_input.get("question_jobs", {}),
        "evidence_registry": graph_input.get("evidence_registry", {}),
        "local_layout": graph_input.get("local_layout", {}),
        "layout_audit": graph_input.get("layout_audit", []),
        "warnings": graph_input.get("warnings", []),
    })
    atomic_write_json(root / "page_quality.json", {
        "schema_version": "1.0",
        "student_hash": root.name,
        "pages": [{"page": page.get("page"), "quality": page.get("quality", {})} for page in pages],
    })
    atomic_write_json(root / "page_evidence.json", {
        "schema_version": "1.0",
        "student_hash": root.name,
        "observations": observations,
        "layout_audit": graph_input.get("layout_audit", []),
        "evidence_registry": graph_input.get("evidence_registry", {}),
    })
    question_results = {
        key: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        for key, value in candidate.question_results.items()
    }
    atomic_write_json(root / "question_reviews.json", {
        "schema_version": "1.0",
        "student_hash": root.name,
        "run_id": candidate.run_id,
        "question_results": question_results,
    })
    atomic_write_json(root / "risk_report.json", {
        "schema_version": "1.0",
        "student_hash": root.name,
        "status": candidate.status.value,
        "overall": candidate.overall.value,
        "unresolved_risk_count": candidate.unresolved_risk_count,
        "risk_question_ids": [
            key for key, value in candidate.question_results.items()
            if value.needs_verification
            or value.risk_level.value != "low"
            or value.verdict.value in {"partial", "incorrect"}
            or value.confidence < 0.75
        ],
        "errors": candidate.errors,
    })
    budget_usage = dict(candidate.budget_usage)
    provider_usage = _provider_usage(provider)
    atomic_write_json(root / "run_audit.json", {
        "schema_version": "1.0",
        "student_hash": root.name,
        "run_id": candidate.run_id,
        "model": str(getattr(provider, "model", "unknown")),
        "provider": provider.__class__.__name__,
        "prompt_version": "grading-prompt-v2-atomic-rubric",
        "preprocess_version": graph_input.get("preprocess_version", RECTIFICATION_VERSION),
        "input_hash": canonical_hash(graph_input),
        "answer_hash": str(answer_manifest.get("answer_hash") or "0" * 64),
        "cache_hits": int(getattr(cache, "hits", 0) or 0),
        "cache_misses": int(getattr(cache, "misses", 0) or 0),
        "elapsed_ms": round(elapsed_ms, 3),
        "budget_usage": budget_usage,
        "provider_usage": provider_usage,
        "local_layout": graph_input.get("local_layout", {}),
        "layout_summary": {
            "accepted_pages": sum(
                item.get("status") == "accepted" for item in graph_input.get("layout_audit", [])
            ),
            "fallback_pages": sum(
                item.get("status") != "accepted" for item in graph_input.get("layout_audit", [])
            ),
        },
        "errors": candidate.errors,
        "langsmith_enabled": False,
    })


def _provider_usage(provider: Any) -> dict[str, int]:
    usage = getattr(provider, "usage", None)
    if usage is None:
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    return {
        "calls": max(0, int(getattr(usage, "calls", 0) or 0)),
        "input_tokens": max(0, int(getattr(usage, "input_tokens", 0) or 0)),
        "output_tokens": max(0, int(getattr(usage, "output_tokens", 0) or 0)),
    }


def _serializable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


def run_student_candidate(
    *,
    provider: Any,
    graph_input: dict[str, Any],
    artifact_root: Path | str,
    checkpoint_path: Path | str | None = None,
    cache_dir: Path | str | None = None,
    pipeline_config: dict[str, Any] | None = None,
) -> CandidateResult:
    """Run one student through the candidate graph and persist candidate-only output.

    The adapter intentionally does not write the legacy ``results/<student>`` files;
    the existing batch runner remains the formal result source during shadow mode.
    """
    # Fail before graph execution if a caller accidentally tries to place credentials
    # in the serializable graph state.
    serialized_input = _serializable(graph_input)
    canonical_json(serialized_input)
    graph_input = migrate_graph_state(serialized_input)
    store = ReviewStore(artifact_root)
    existing = store.load_candidate(str(graph_input.get("student_id", "")))
    if (
        existing is not None
        and existing.run_id == str(graph_input.get("run_id", "run-unknown"))
        and existing.assignment_id == str(graph_input.get("assignment_id", "assignment-unknown"))
    ):
        return existing
    checkpoint_context = nullcontext(None)
    if checkpoint_path is not None:
        from app.grading_graph.checkpoint import open_sqlite_checkpointer

        checkpoint_context = open_sqlite_checkpointer(checkpoint_path)
    started = time.perf_counter()
    from app.grading_graph.cache import JsonResponseCache
    from app.grading_graph.budget import RateLimitedJsonProvider

    cache = JsonResponseCache(cache_dir) if cache_dir is not None else None
    limited_provider = provider if isinstance(provider, RateLimitedJsonProvider) else RateLimitedJsonProvider(provider)
    with checkpoint_context as checkpointer:
        app = build_grading_graph(
            limited_provider,
            checkpointer=checkpointer,
            cache=cache,
            execution_settings=GraphExecutionSettings.from_pipeline_config(pipeline_config),
        )
        config = {"configurable": {"thread_id": str(graph_input.get("run_id", "run-unknown"))}}
        output = app.invoke(graph_input, config=config) if checkpointer is not None else app.invoke(graph_input)
    candidate = CandidateResult.model_validate(output["candidate"])
    store.save_candidate(candidate)
    _persist_pipeline_artifacts(
        artifact_root=artifact_root,
        student_id=str(graph_input.get("student_id", "")),
        graph_input=graph_input,
        candidate=candidate,
        provider=provider,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        cache=cache,
    )
    return candidate


def run_student_candidate_from_images(
    *,
    provider: Any,
    processed_student_dir: Path | str,
    answer_manifest_path: Path | str,
    artifact_root: Path | str,
    assignment_id: str,
    student_id: str,
    run_id: str,
    budget: Budget,
    checkpoint_path: Path | str | None = None,
    cache_dir: Path | str | None = None,
    local_layout_config: dict[str, Any] | None = None,
    local_layout_backend: Any = None,
    question_label_reader: Any = None,
    pipeline_config: dict[str, Any] | None = None,
) -> CandidateResult:
    """Run the checkpointed parent graph from processed pages to candidate."""
    store = ReviewStore(artifact_root)
    existing = store.load_candidate(student_id)
    if (
        existing is not None
        and existing.run_id == run_id
        and existing.assignment_id == assignment_id
    ):
        return existing

    from app.grading_graph.budget import BudgetLedger
    from app.grading_graph.budget import RateLimitedJsonProvider
    from app.grading_graph.cache import JsonResponseCache
    ledger = BudgetLedger(budget.model_dump(mode="json"))
    cache = JsonResponseCache(cache_dir) if cache_dir is not None else None
    limited_provider = provider if isinstance(provider, RateLimitedJsonProvider) else RateLimitedJsonProvider(provider)
    settings = GraphExecutionSettings.from_pipeline_config(pipeline_config)
    started = time.perf_counter()
    launch_state = {
        "schema_version": "1.0",
        "graph_version": "langgraph-v3-evidence-first",
        "run_id": run_id,
        "assignment_id": assignment_id,
        "student_id": student_id,
        "budget": budget.model_dump(mode="json"),
        "processed_student_dir": str(Path(processed_student_dir).resolve()),
        "answer_manifest_path": str(Path(answer_manifest_path).resolve()),
        "artifact_root": str(Path(artifact_root).resolve()),
        "local_layout_config": dict(
            local_layout_config
            if local_layout_config is not None
            else (pipeline_config or {}).get("local_layout") or {}
        ),
    }
    checkpoint_context = nullcontext(None)
    if checkpoint_path is not None:
        from app.grading_graph.checkpoint import open_sqlite_checkpointer

        checkpoint_context = open_sqlite_checkpointer(checkpoint_path)
    with checkpoint_context as checkpointer:
        app = build_image_grading_graph(
            limited_provider,
            checkpointer=checkpointer,
            cache=cache,
            budget_ledger=ledger,
            execution_settings=settings,
            local_layout_backend=local_layout_backend,
            question_label_reader=question_label_reader,
        )
        config = {"configurable": {"thread_id": run_id}}
        output = app.invoke(launch_state, config=config) if checkpointer is not None else app.invoke(launch_state)
    candidate = CandidateResult.model_validate(output["candidate"])
    candidate = _recover_provider_questions(candidate, existing)
    store.save_candidate(candidate)
    graph_input = graph_state_payload(output)
    for transient_key in ("candidate", "question_results", "risk_question_ids", "budget_usage"):
        graph_input.pop(transient_key, None)
    _persist_pipeline_artifacts(
        artifact_root=artifact_root,
        student_id=student_id,
        graph_input=graph_input,
        candidate=candidate,
        provider=provider,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        cache=cache,
    )
    return candidate


def run_candidate_states(
    *,
    provider: Any,
    states: Iterable[dict[str, Any]],
    artifact_root: Path | str,
    checkpoint_dir: Path | str,
    cache_dir: Path | str | None = None,
    max_students: int = 0,
    pipeline_config: dict[str, Any] | None = None,
) -> CandidateBatchSummary:
    """Run prepared states sequentially with durable, candidate-only writes."""
    checkpoint_dir = Path(checkpoint_dir).resolve()
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    processed = succeeded = failed = 0
    failures: list[dict[str, str]] = []
    consecutive_provider_errors = 0
    stop_reason: str | None = None
    for state in states:
        if max_students and processed >= max_students:
            break
        processed += 1
        assignment_id = str(state.get("assignment_id", "assignment-unknown"))
        checkpoint_name = canonical_json({"assignment_id": assignment_id})
        import hashlib

        checkpoint_hash = hashlib.sha256(checkpoint_name.encode("utf-8")).hexdigest()[:16]
        try:
            candidate = run_student_candidate(
                provider=provider,
                graph_input=state,
                artifact_root=artifact_root,
                checkpoint_path=checkpoint_dir / f"grading-{checkpoint_hash}.sqlite",
                cache_dir=cache_dir,
                pipeline_config=pipeline_config,
            )
            succeeded += 1
            if candidate_has_provider_error(candidate):
                consecutive_provider_errors += 1
            else:
                consecutive_provider_errors = 0
            if consecutive_provider_errors >= 3:
                stop_reason = "three_consecutive_provider_errors"
                break
        except Exception as exc:
            # Callers can report a count without accidentally printing provider
            # request data or SDK exception text.
            failed += 1
            failures.append(
                {
                    "student_hash": hashlib.sha256(str(state.get("student_id", "")).encode("utf-8")).hexdigest(),
                    "error_type": type(exc).__name__,
                }
            )
            if _is_provider_error_type(type(exc).__name__):
                consecutive_provider_errors += 1
                if consecutive_provider_errors >= 3:
                    stop_reason = "three_consecutive_provider_errors"
                    break
            else:
                consecutive_provider_errors = 0
    return CandidateBatchSummary(
        processed=processed,
        succeeded=succeeded,
        failed=failed,
        failures=tuple(failures),
        stop_reason=stop_reason,
    )
