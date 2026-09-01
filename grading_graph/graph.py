from __future__ import annotations

import operator
import re
import threading
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from PIL import Image, ImageEnhance, ImageFilter

from grading_graph.nodes.aggregator import build_candidate
from grading_graph.budget import BudgetLedger
from grading_graph.cache import CachedJsonProvider, JsonResponseCache
from grading_graph.nodes.evidence_gate import build_evidence_registry
from grading_graph.nodes.grader import GradingProvider, QuestionGrader
from grading_graph.nodes.math_checks import apply_deterministic_math_checks
from grading_graph.nodes.question_locator import QuestionLocator
from grading_graph.nodes.rubric_compiler import compile_atomic_rubrics, deterministic_rubric_verdict
from grading_graph.nodes.symbol_auditor import SymbolAuditor
from grading_graph.nodes.verifier import TargetedVerifier
from grading_graph.schemas import EvidenceRef, PageArtifact, QuestionJob, QuestionResult, QuestionVerdict, RiskLevel, StudentStatus


class _ConcurrencyLimitedProvider:
    """Bound concurrent multimodal calls to avoid burst rate-limit failures.

    LangGraph fans out one ``Send`` per question.  Without a small gate this
    can issue a dozen DashScope requests at once, which is especially brittle
    for a single shared API key.  The gate is deliberately local to one graph
    instance and does not alter prompts or retry semantics.
    """

    def __init__(self, provider: GradingProvider, max_concurrency: int = 2) -> None:
        self.provider = provider
        self._semaphore = threading.BoundedSemaphore(max(1, int(max_concurrency)))

    def complete_json(self, prompt: str, schema: dict[str, Any], image_ref: str | None = None) -> dict[str, Any]:
        with self._semaphore:
            return self.provider.complete_json(prompt, schema, image_ref=image_ref)


def _merge_dicts(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def _safe_error(stage: str, question_id: str, exc: Exception) -> dict[str, str]:
    """Keep provider and filesystem exception text out of persisted artifacts."""
    return {
        "stage": stage,
        "question_id": question_id,
        "error_type": type(exc).__name__,
    }


def _filter_incompatible_transcriptions(
    values: list[dict[str, Any]] | list[Any],
    evidence_packet: dict[str, Any],
) -> list[Any]:
    """Remove only evidence fragments confidently bound to a neighbor."""

    if evidence_packet.get("semantic_compatible") is False or evidence_packet.get("ambiguous_route"):
        return []
    incompatible_ids = {
        str(value) for value in (evidence_packet.get("incompatible_span_ids") or [])
    }
    if not incompatible_ids:
        return list(values)
    filtered: list[Any] = []
    for value in values:
        span_id = str(value.get("span_id", "")) if isinstance(value, dict) else str(getattr(value, "span_id", ""))
        if span_id not in incompatible_ids:
            filtered.append(value)
    return filtered


def _question_image_refs(job: QuestionJob, *, include_enhanced: bool = False) -> str | list[str] | None:
    """Return the bounded set of page images needed to audit one question.

    A question can span multiple photographed pages.  Sending only the first
    ROI made the grader/verifier treat a complete multi-page answer as
    truncated.  Keep one image as a string for backwards compatibility with
    lightweight providers/tests, and pass up to four distinct pages when the
    job really spans more than one page.
    """
    refs: list[str] = []
    for ref in job.roi_refs:
        path = str(ref.artifact_ref)
        if path and Path(path).is_file() and path not in refs:
            refs.append(path)
        if include_enhanced and path:
            enhanced = Path(path).with_name("enhanced.png")
            enhanced_path = str(enhanced)
            if enhanced.is_file() and enhanced_path not in refs:
                refs.append(enhanced_path)
    if not refs:
        return None
    return refs[0] if len(refs) == 1 else refs[:4]


def _bounded_full_page_job(job: QuestionJob, raw_pages: list[Any]) -> QuestionJob:
    """Expand an incomplete compound answer to all available page images."""

    refs: list[EvidenceRef] = []
    for raw in raw_pages:
        try:
            page = PageArtifact.model_validate(raw)
        except ValueError:
            continue
        source = page.normalized or page.original
        path = Path(source.path)
        if page.page_type == "blank" or not path.is_file():
            continue
        try:
            with Image.open(path) as image:
                width, height = image.size
        except OSError:
            continue
        refs.append(
            EvidenceRef(
                span_id=f"compound-full-page-{page.page}",
                page=page.page,
                bbox=(0, 0, width, height),
                artifact_ref=str(path),
                view="normalized" if page.normalized is not None else "original",
            )
        )
        if len(refs) >= 4:
            break
    if not refs:
        return job
    return job.model_copy(
        update={
            "pages": [ref.page for ref in refs],
            "roi_refs": refs,
            "route": "risk",
        }
    )


def _symbol_audit_image_refs(span: Any, job: QuestionJob) -> str | list[str] | None:
    """Materialize small normalized/enhanced crops for critical-symbol rereads."""

    source_ref = next((ref for ref in job.roi_refs if ref.page == span.page), None)
    if source_ref is None:
        return None
    source = Path(source_ref.artifact_ref)
    if not source.is_file():
        return None
    try:
        with Image.open(source) as image:
            image.load()
            x1, y1, x2, y2 = (int(value) for value in span.bbox)
            pad_x = max(24, int((x2 - x1) * 0.12))
            pad_y = max(24, int((y2 - y1) * 0.18))
            box = (
                max(0, x1 - pad_x),
                max(0, y1 - pad_y),
                min(image.width, x2 + pad_x),
                min(image.height, y2 + pad_y),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                return str(source)
            crop = image.convert("RGB").crop(box)
            if crop.width < 1200:
                scale = min(4.0, 1200 / max(1, crop.width))
                crop = crop.resize(
                    (max(1, int(crop.width * scale)), max(1, int(crop.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            safe_id = re.sub(r"[^0-9A-Za-z._-]+", "_", str(span.span_id)).strip("_") or "span"
            normalized_path = source.with_name(f"symbol_{safe_id}_normalized.png")
            enhanced_path = source.with_name(f"symbol_{safe_id}_enhanced.png")
            temporary = normalized_path.with_suffix(".tmp.png")
            crop.save(temporary, format="PNG")
            temporary.replace(normalized_path)
            enhanced = ImageEnhance.Contrast(crop).enhance(1.55).filter(
                ImageFilter.UnsharpMask(radius=1.6, percent=180, threshold=2)
            )
            temporary = enhanced_path.with_suffix(".tmp.png")
            enhanced.save(temporary, format="PNG")
            temporary.replace(enhanced_path)
            return [str(normalized_path), str(enhanced_path)]
    except OSError:
        return str(source)


class GradingGraphState(TypedDict, total=False):
    schema_version: str
    graph_version: str
    run_id: str
    assignment_id: str
    student_id: str
    answer_manifest: dict[str, Any]
    pages: list[dict[str, Any]]
    page_observations: list[dict[str, Any]]
    local_layout: dict[str, Any]
    layout_audit: list[dict[str, Any]]
    question_jobs: dict[str, QuestionJob | dict[str, Any]]
    transcriptions: dict[str, list[dict[str, Any]]]
    answer_texts: dict[str, str]
    evidence_registry: dict[str, dict[str, Any]]
    question_ids: list[str]
    question_results: Annotated[dict[str, dict[str, Any]], _merge_dicts]
    ambiguities: list[dict[str, Any]]
    errors: Annotated[list[dict[str, Any]], operator.add]
    warnings: list[dict[str, Any]]
    budget: dict[str, Any]
    budget_usage: dict[str, int]
    retries: dict[str, int]
    audit: dict[str, Any]
    final_projection: dict[str, Any]
    risk_question_ids: list[str]
    candidate: dict[str, Any]


CURRENT_GRAPH_SCHEMA_VERSION = "1.0"


def migrate_graph_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize durable state before any node reads it.

    Version 0.9 was the pre-manifest state shape.  Its fields are compatible
    with the current graph, so migration is additive and idempotent.  Future
    versions fail closed instead of being silently interpreted as older data.
    """
    normalized = dict(state or {})
    raw_version = normalized.get("schema_version", "0.9")
    version = str(raw_version)
    if version not in {"0.9", CURRENT_GRAPH_SCHEMA_VERSION}:
        raise ValueError(f"unsupported graph state schema version: {version}")
    normalized["schema_version"] = CURRENT_GRAPH_SCHEMA_VERSION
    for key, default in (
        ("question_results", {}),
        ("ambiguities", []),
        ("errors", []),
        ("retries", {}),
        ("evidence_registry", {}),
        ("local_layout", {}),
        ("layout_audit", []),
        ("warnings", []),
    ):
        normalized.setdefault(key, default)
    return normalized


def build_grading_graph(
    provider: GradingProvider,
    *,
    checkpointer: Any = None,
    cache: JsonResponseCache | None = None,
    cache_dir: Any = None,
    budget_ledger: BudgetLedger | None = None,
    max_retries: int = 2,
    max_provider_concurrency: int = 2,
):
    if cache is not None and cache_dir is not None:
        raise ValueError("pass either cache or cache_dir, not both")
    response_cache = cache or (JsonResponseCache(cache_dir) if cache_dir is not None else None)
    # Keep fan-out parallelism in LangGraph while serializing the expensive
    # upstream calls to a small bounded pool.  This prevents transient 429/5xx
    # bursts from turning otherwise clear transcriptions into unreadable
    # results, without changing the graph's evidence gates.
    limited_provider = _ConcurrencyLimitedProvider(provider, max_provider_concurrency)
    ledgers: dict[str, BudgetLedger] = {}

    def get_ledger(state: dict[str, Any]) -> BudgetLedger:
        run_id = str(state.get("run_id", "run-unknown"))
        if run_id not in ledgers:
            if budget_ledger is not None:
                ledgers[run_id] = budget_ledger
                return budget_ledger
            limits = state.get("budget") or {
                "max_calls": 1000,
                "max_input_tokens": 1_000_000,
                "max_output_tokens": 1_000_000,
            }
            ledgers[run_id] = BudgetLedger(limits)
        return ledgers[run_id]

    def prepare(state: GradingGraphState) -> dict[str, Any]:
        state = migrate_graph_state(state)
        jobs = state.get("question_jobs", {})
        normalized_jobs = {
            str(question_id): QuestionJob.model_validate(job).model_dump(mode="json")
            for question_id, job in jobs.items()
        }
        normalized_transcriptions = {
            str(question_id): [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                for item in spans
            ]
            for question_id, spans in state.get("transcriptions", {}).items()
        }
        normalized_answer_texts = {
            str(question_id): str(text)
            for question_id, text in state.get("answer_texts", {}).items()
        }
        get_ledger(state)
        return {
            "schema_version": CURRENT_GRAPH_SCHEMA_VERSION,
            "question_jobs": normalized_jobs,
            "transcriptions": normalized_transcriptions,
            "answer_texts": normalized_answer_texts,
            "question_ids": sorted(normalized_jobs),
            "local_layout": dict(state.get("local_layout") or {}),
            "layout_audit": list(state.get("layout_audit") or []),
            "warnings": list(state.get("warnings") or []),
        }

    def coverage_gate(state: GradingGraphState) -> dict[str, Any]:
        """Freeze answer-blind routing/OCR coverage before any grading call."""
        return {
            "evidence_registry": build_evidence_registry(
                state.get("question_jobs", {}),
                state.get("transcriptions", {}),
            )
        }

    def dispatch_grading(state: GradingGraphState):
        jobs = state.get("question_jobs", {})
        ids = state.get("question_ids", sorted(jobs))
        if not ids:
            return "aggregate"
        return [
            Send(
                "grade_question",
                {
                    "question_id": question_id,
                    "job": jobs[question_id],
                    "transcription": state.get("transcriptions", {}).get(question_id, []),
                    "answer_text": state.get("answer_texts", {}).get(question_id, ""),
                    "evidence_packet": state.get("evidence_registry", {}).get(question_id, {}),
                    "run_id": state.get("run_id", "run-unknown"),
                },
            )
            for question_id in ids
        ]

    def grade_question(state: dict[str, Any]) -> dict[str, Any]:
        question_id = str(state["question_id"])
        audited_spans = []
        evidence_packet = dict(state.get("evidence_packet") or {})
        try:
            job = QuestionJob.model_validate(state["job"])
            if job.route == "mismatch":
                result = QuestionResult(
                    question_id=question_id,
                    verdict="mismatch",
                    confidence=1.0,
                    needs_verification=False,
                    risk_level=RiskLevel.LOW,
                    evidence_status="mismatch",
                    resolution_status="not_applicable",
                )
                return {"question_results": {question_id: result.model_dump(mode="json")}}
            if job.route == "unreadable" and not job.roi_refs:
                result = QuestionResult(
                    question_id=question_id,
                    verdict="unreadable",
                    confidence=0.0,
                    needs_verification=True,
                    risk_level=RiskLevel.HIGH,
                    evidence_status="missing_route",
                    resolution_status="needs_rescue",
                    attempt_history=[{"stage": "coverage_gate", "outcome": "missing_route"}],
                )
                return {"question_results": {question_id: result.model_dump(mode="json")}}
            if job.route == "unreadable" and job.roi_refs:
                job = job.model_copy(update={"route": "risk"})
            question_provider = _budgeted_provider(get_ledger(state))
            locator_outcome: str | None = None
            is_full_page_fallback = bool(job.roi_refs) and all(
                ref.span_id.startswith("fallback-page-") for ref in job.roi_refs
            )
            incompatible_transcription = evidence_packet.get("semantic_compatible") is False
            ambiguous_route = bool(evidence_packet.get("ambiguous_route"))
            incomplete_subparts = evidence_packet.get("subpart_coverage_complete") is False
            # If the evidence already proves that only one of several
            # explicit subparts was transcribed, a tighter crop is unsafe: a
            # locator can easily lock onto the wrong bare ``(2)`` elsewhere
            # on one routed page. Expand to all bounded pages first, then keep
            # both the located crop and its full-page context.
            if incomplete_subparts:
                rescue_job = _bounded_full_page_job(job, list(state.get("pages", [])))
                job = rescue_job
                try:
                    located_job = QuestionLocator(question_provider).locate_and_crop(rescue_job)
                    if located_job is not None:
                        # Keep every bounded full page in reading order.  The
                        # answer-blind locator can select the wrong bare ``(2)``;
                        # the grader's reference-aware evidence check must still
                        # be able to find the true page. Add the compound crop
                        # only if the four-image cap leaves room.
                        combined = [*rescue_job.roi_refs, *located_job.roi_refs]
                        job = located_job.model_copy(
                            update={
                                "pages": sorted({ref.page for ref in combined}),
                                "roi_refs": combined[:4],
                            }
                        )
                        locator_outcome = "located_with_full_page_context"
                    else:
                        locator_outcome = "full_page_subpart_rescue"
                except Exception:
                    locator_outcome = "full_page_subpart_rescue_provider_error"
            elif (
                is_full_page_fallback
                or incompatible_transcription
                or ambiguous_route
                or evidence_packet.get("status") == "incomplete"
            ):
                try:
                    located_job = QuestionLocator(question_provider).locate_and_crop(job)
                    if located_job is not None:
                        job = located_job
                        locator_outcome = "located"
                    else:
                        locator_outcome = "not_found"
                except Exception:
                    locator_outcome = "provider_error"
            atomic_rubrics = compile_atomic_rubrics(job, str(state.get("answer_text", "")))
            if job.answer_slice is not None and atomic_rubrics:
                job = job.model_copy(
                    update={
                        "answer_slice": job.answer_slice.model_copy(
                            update={"rubric_items": atomic_rubrics}
                        )
                    }
                )
            audit_warnings: list[dict[str, Any]] = []
            # When the evidence gate detects a high-confidence neighboring
            # question, do not let that text leak back into the grader after
            # the locator has found the actual target region.
            transcription_values = _filter_incompatible_transcriptions(
                state.get("transcription", []),
                evidence_packet,
            )
            if incomplete_subparts:
                transcription_values = []
            from grading_graph.schemas import SymbolCandidate, TranscriptionSpan

            parsed_spans = [TranscriptionSpan.model_validate(value) for value in transcription_values]
            audit_candidates = [
                span
                for span in parsed_spans
                if bool(evidence_packet.get("symbol_audit_required"))
                and (
                    span.readability != "clear"
                    or any(candidate.symbol != "unknown" for candidate in span.symbol_candidates)
                )
            ]
            # One carefully selected original-image reread per question gives
            # most of the sign benefit without multiplying calls by every OCR
            # line. Prefer uncertain spans, final expressions/conclusions and
            # lower-page spans where answers are usually finalized.
            selected_audit_ids = {
                span.span_id
                for span in sorted(
                    audit_candidates,
                    key=lambda value: (
                        value.readability != "clear",
                        bool(re.search(r"(?:x_?1|x_?2|通解|结论|主列|自由列|β|beta)", value.text, re.IGNORECASE)),
                        value.bbox[3],
                    ),
                    reverse=True,
                )[:1]
            }
            for span in parsed_spans:
                # Symbol audit is a targeted escalation. Clear spans without
                # explicit symbol ambiguity do not need a second multimodal
                # call; reserve that budget for uncertain/negative-sign cases.
                if span.span_id not in selected_audit_ids:
                    audited_spans.append(span)
                    continue
                image_ref = _symbol_audit_image_refs(span, job)
                try:
                    span = SymbolAuditor(question_provider, max_rounds=1).audit(span, image_ref=image_ref)
                except Exception as exc:
                    # Symbol audit is an auxiliary OCR check, not the source
                    # of truth.  A transient provider failure must not poison
                    # the whole question or preserve text that we already
                    # considered sign-unsafe.  Blank that text and force the
                    # grader/verifier to use the original image instead.
                    audit_warnings.append(
                        {
                            "stage": "symbol_auditor",
                            "outcome": "provider_unavailable_original_image_fallback",
                            "span_id": span.span_id,
                            "error_type": type(exc).__name__,
                        }
                    )
                    span = span.model_copy(
                        update={
                            "text": "",
                            "symbol_candidates": [
                                SymbolCandidate(symbol="unknown", confidence=0.0)
                            ],
                            "readability": "uncertain",
                            "confidence": 0.0,
                        }
                    )
                audited_spans.append(span)
            grader_image_ref = _question_image_refs(
                job,
                include_enhanced=bool(evidence_packet.get("symbol_audit_required")),
            )
            result = QuestionGrader(
                question_provider,
                max_retries=max_retries,
                backoff_base=0.25,
                strict_evidence_gate=False,
            ).grade(
                job,
                audited_spans,
                answer_text=state.get("answer_text", ""),
                image_ref=grader_image_ref,
            )
            result = apply_deterministic_math_checks(job, result)
            if locator_outcome == "located" and not result.evidence_refs:
                result = result.model_copy(update={"evidence_refs": list(job.roi_refs)})
            deterministic_verdict = deterministic_rubric_verdict(
                list(result.rubric_decisions),
                [str(item.get("id") or item.get("rubric_id") or "") for item in atomic_rubrics],
            )
            if deterministic_verdict is not None:
                result = result.model_copy(update={"verdict": QuestionVerdict(deterministic_verdict)})
            rescue_required = bool(evidence_packet.get("requires_rescue"))
            symbol_verification_required = bool(evidence_packet.get("symbol_audit_required"))
            result = result.model_copy(
                update={
                    "needs_verification": result.needs_verification or symbol_verification_required,
                    "risk_level": RiskLevel.HIGH if symbol_verification_required else result.risk_level,
                    "evidence_status": str(evidence_packet.get("status") or "ready"),
                    "resolution_status": "rescued" if rescue_required else "graded",
                    "attempt_history": (
                        ([{"stage": "question_locator", "outcome": locator_outcome}] if locator_outcome else [])
                        + [
                        {
                            "stage": "coverage_gate",
                            "outcome": str(evidence_packet.get("status") or "ready"),
                        },
                        {
                            "stage": "question_grader",
                            "outcome": "completed",
                            "atomic_rubric_count": len(atomic_rubrics),
                        },
                        *audit_warnings,
                        ]
                    ),
                }
            )
            output: dict[str, Any] = {"question_results": {question_id: result.model_dump(mode="json")}}
            return output
        except Exception as exc:
            # Preserve any successful transcription when the grader call
            # itself fails.  Dropping those spans used to create an empty
            # unreadable result, which meant the targeted verifier had no
            # image-backed evidence with which to recover a transient grader
            # failure.  A bounded evidence set lets the verifier make a real
            # second-pass decision without weakening the evidence gate.
            fallback_refs: list[EvidenceRef] = []
            try:
                job = QuestionJob.model_validate(state.get("job"))
                page_rois = {ref.page: ref for ref in job.roi_refs}
                for span in audited_spans:
                    roi = page_rois.get(span.page)
                    if roi is None:
                        continue
                    fallback_refs.append(
                        EvidenceRef(
                            span_id=span.span_id,
                            page=span.page,
                            bbox=span.bbox,
                            artifact_ref=roi.artifact_ref,
                            view=roi.view,
                        )
                    )
            except Exception:
                fallback_refs = []
            failed = QuestionResult(
                question_id=question_id,
                verdict="unreadable",
                confidence=0,
                needs_verification=True,
                risk_level=RiskLevel.CRITICAL,
                evidence_refs=fallback_refs,
                transcription=audited_spans,
                evidence_status="provider_error",
                resolution_status="provider_failed",
                attempt_history=[
                    {
                        "stage": "coverage_gate",
                        "outcome": str(evidence_packet.get("status") or "unknown"),
                    },
                    {"stage": "question_grader", "outcome": "provider_error"},
                ],
            )
            return {
                "question_results": {question_id: failed.model_dump(mode="json")},
                "errors": [_safe_error("grader", question_id, exc)],
            }

    def aggregate(state: GradingGraphState) -> dict[str, Any]:
        results = {key: QuestionResult.model_validate(value) for key, value in state.get("question_results", {}).items()}
        # A fast-path grader is allowed to propose a result, but a low-risk
        # label is not proof that the proposal is correct. Vision models often
        # omit/understate confidence while still emitting a crisp-looking
        # verdict. Route deductions and low-confidence proposals through the
        # targeted verifier so the adversarial loop can catch partial work,
        # parameter-branch mistakes, and arithmetic slips. Empty-evidence /
        # unreadable items are filtered in dispatch_verification and remain
        # abstentions rather than becoming scored results.
        risk_ids = sorted(
            key
            for key, result in results.items()
            if result.needs_verification
            or result.risk_level != RiskLevel.LOW
            or result.verdict.value in {"partial", "incorrect"}
            or result.confidence < 0.75
        )
        return {"question_results": {key: value.model_dump(mode="json") for key, value in results.items()}, "risk_question_ids": risk_ids}

    def dispatch_verification(state: GradingGraphState):
        ids = state.get("risk_question_ids", [])
        # Do not spend another model call on an unreadable/mismatch branch when
        # no transcription or image-backed ROI exists.  There is no evidence a
        # verifier can inspect in that case; preserving the abstention is safer
        # and leaves budget for evidence-bearing questions.
        filtered_ids = []
        for question_id in ids:
            result = state.get("question_results", {}).get(question_id, {})
            has_transcription = bool(state.get("transcriptions", {}).get(question_id, []))
            job_value = state.get("question_jobs", {}).get(question_id)
            has_roi = bool(QuestionJob.model_validate(job_value).roi_refs) if job_value else False
            if result.get("verdict") == "unreadable" and not has_transcription and not has_roi:
                continue
            filtered_ids.append(question_id)
        ids = filtered_ids
        if not ids:
            return "finalize"
        sends = []
        for question_id in ids:
            answer_text = state.get("answer_texts", {}).get(question_id, "")
            job_value = state.get("question_jobs", {}).get(question_id)
            verifier_job = QuestionJob.model_validate(job_value) if job_value else None
            if verifier_job is not None and verifier_job.answer_slice is not None:
                atomic_rubrics = compile_atomic_rubrics(verifier_job, str(answer_text))
                if atomic_rubrics:
                    verifier_job = verifier_job.model_copy(
                        update={
                            "answer_slice": verifier_job.answer_slice.model_copy(
                                update={"rubric_items": atomic_rubrics}
                            )
                        }
                    )
            evidence_packet = state.get("evidence_registry", {}).get(question_id, {})
            verifier_transcription = _filter_incompatible_transcriptions(
                state.get("transcriptions", {}).get(question_id, []),
                evidence_packet,
            )
            sends.append(Send(
                "verify_question",
                {
                    "question_id": question_id,
                    "result": state["question_results"][question_id],
                    "job": verifier_job.model_dump(mode="json") if verifier_job is not None else None,
                    "transcription": verifier_transcription,
                    "answer_text": answer_text,
                    "evidence_packet": evidence_packet,
                    "run_id": state.get("run_id", "run-unknown"),
                },
            ))
        return sends

    def verify_question(state: GradingGraphState) -> dict[str, Any]:
        question_id = str(state["question_id"])
        current = QuestionResult.model_validate(state["result"])
        try:
            job = QuestionJob.model_validate(state["job"]) if state.get("job") else None
            evidence_packet = dict(state.get("evidence_packet") or {})
            image_ref = _question_image_refs(
                job,
                include_enhanced=bool(evidence_packet.get("symbol_audit_required")),
            ) if job is not None else None
            if current.resolution_status == "rescued":
                rescued_paths: list[str] = []
                for ref in current.evidence_refs:
                    path = str(ref.artifact_ref)
                    if Path(path).is_file() and "located_" in Path(path).name and path not in rescued_paths:
                        rescued_paths.append(path)
                if rescued_paths:
                    image_ref = rescued_paths[0] if len(rescued_paths) == 1 else rescued_paths[:2]
            updated = TargetedVerifier(_budgeted_provider(get_ledger(state))).verify(
                current,
                job=job,
                transcription=state.get("transcription", []),
                answer_text=str(state.get("answer_text", "")),
                image_ref=image_ref,
            )
            return {"question_results": {question_id: updated.model_dump(mode="json")}}
        except Exception as exc:
            failed = current.model_copy(
                update={
                    "needs_verification": True,
                    "risk_level": RiskLevel.CRITICAL,
                    "verifier_result": {"decisive": False, "error_type": type(exc).__name__},
                }
            )
            return {
                "question_results": {question_id: failed.model_dump(mode="json")},
                "errors": [_safe_error("verifier", question_id, exc)],
            }

    def finalize(state: GradingGraphState) -> dict[str, Any]:
        ledger = ledgers.get(str(state.get("run_id", "run-unknown")))
        budget_usage: dict[str, int] = {}
        if ledger is not None:
            snapshot = ledger.snapshot
            budget_usage = {
                "calls": snapshot.calls,
                "input_tokens": snapshot.input_tokens,
                "output_tokens": snapshot.output_tokens,
            }
        candidate = build_candidate(
            graph_version=state.get("graph_version", "langgraph-v3-evidence-first"),
            run_id=state.get("run_id", "run-unknown"),
            assignment_id=state.get("assignment_id", "assignment-unknown"),
            student_id=state.get("student_id", "student-unknown"),
            question_results=state.get("question_results", {}),
            errors=state.get("errors", []),
            budget_usage=budget_usage,
        )
        result = {"candidate": candidate.model_dump(mode="json")}
        if ledger is not None:
            result["budget_usage"] = budget_usage
            ledgers.pop(str(state.get("run_id", "run-unknown")), None)
        return result

    def _budgeted_provider(ledger: BudgetLedger):
        from grading_graph.budget import BudgetedJsonProvider

        paid_provider: Any = BudgetedJsonProvider(limited_provider, ledger)
        # Cache must be the outermost wrapper: a cache hit is neither a paid
        # provider call nor fresh token consumption.  The previous order
        # reserved budget before consulting the cache and could starve the
        # verifier even when most logical calls were free cache hits.
        return CachedJsonProvider(paid_provider, response_cache) if response_cache else paid_provider

    builder = StateGraph(GradingGraphState)
    builder.add_node("prepare", prepare)
    builder.add_node("coverage_gate", coverage_gate)
    builder.add_node("grade_question", grade_question)
    builder.add_node("aggregate", aggregate)
    builder.add_node("verify_question", verify_question)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "coverage_gate")
    builder.add_conditional_edges("coverage_gate", dispatch_grading)
    builder.add_edge("grade_question", "aggregate")
    builder.add_conditional_edges("aggregate", dispatch_verification)
    builder.add_edge("verify_question", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)
