from __future__ import annotations

import re
from typing import Any, Iterable

from app.grading_graph.schemas import QuestionJob, TranscriptionSpan


EVIDENCE_READY = "ready"
EVIDENCE_IMAGE_ONLY = "image_only"
EVIDENCE_INCOMPLETE = "incomplete"
EVIDENCE_MISSING_ROUTE = "missing_route"
EVIDENCE_MISMATCH = "mismatch"


def _incompatible_span_ids(
    question_job: QuestionJob,
    spans: list[TranscriptionSpan],
) -> list[str]:
    """Identify only high-confidence neighboring-question fragments.

    This is intentionally a per-span filter rather than a whole-question
    rejection.  A page can contain the end of the target answer and the start
    of the next exercise; throwing away every span lets the neighbor overwrite
    valid evidence.  The rule is enabled only when the question metadata says
    the target is the RREF/pivot/free-column task.
    """

    answer_slice = question_job.answer_slice
    if answer_slice is None:
        return []
    metadata = "\n".join(
        [
            str(answer_slice.heading or ""),
            str(answer_slice.problem or ""),
            str(answer_slice.reference_answer or ""),
            *[
                str(item.get("requirement") or item.get("description") or "")
                for item in answer_slice.rubric_items
                if isinstance(item, dict)
            ],
        ]
    )
    if "主列" not in metadata or "自由列" not in metadata:
        return []

    incompatible: list[str] = []
    for span in spans:
        text = str(span.text or "")
        beta_alpha_expression = bool(
            re.search(r"(?:β|\\beta|\bbeta\b).{0,120}(?:α|\\alpha|\balpha\b)", text, re.IGNORECASE | re.DOTALL)
            or re.search(r"(?:α|\\alpha|\balpha\b).{0,120}(?:β|\\beta|\bbeta\b)", text, re.IGNORECASE | re.DOTALL)
        )
        if beta_alpha_expression or re.search(r"线性(?:表示|组合)", text):
            incompatible.append(span.span_id)
    return incompatible


def _semantic_compatibility(question_job: QuestionJob, spans: list[TranscriptionSpan]) -> bool | None:
    """Detect a narrow, high-confidence wrong-question evidence binding.

    This gate deliberately uses question metadata rather than the reference
    solution.  A rank proof must contain at least one rank/proof anchor; a span
    containing only an unrelated parameter calculation cannot safely support
    it.  Other question families remain unspecified instead of receiving a
    brittle keyword rule.
    """

    answer_slice = question_job.answer_slice
    if answer_slice is None or not spans:
        return None
    question_type = str(answer_slice.question_type or question_job.question_type or "").lower()
    checks = {str(value).lower() for value in answer_slice.deterministic_checks}
    if question_type != "proof" or "rank" not in checks:
        return None
    text = "\n".join(span.text for span in spans)
    return bool(
        re.search(
            r"(?:秩|增广|证明|rank|r\s*\(|r\s*['′])",
            text,
            flags=re.IGNORECASE,
        )
    )


def build_evidence_packet(
    job: QuestionJob | dict[str, Any],
    transcription: Iterable[TranscriptionSpan | dict[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic, answer-blind evidence contract for one question.

    The grading model must not be responsible for deciding whether routing or
    OCR succeeded.  This packet records those facts before the answer slice is
    consulted, which keeps provider failures distinct from genuinely
    unreadable handwriting and gives the graph a stable rescue decision.
    """

    question_job = QuestionJob.model_validate(job)
    spans = [TranscriptionSpan.model_validate(item) for item in transcription]
    incompatible_span_ids = _incompatible_span_ids(question_job, spans)
    incompatible_set = set(incompatible_span_ids)
    compatible_spans = [span for span in spans if span.span_id not in incompatible_set]
    roi_pages = sorted({ref.page for ref in question_job.roi_refs})
    span_pages = sorted({span.page for span in spans})
    compatible_span_pages = sorted({span.page for span in compatible_spans})
    clear_spans = [span for span in compatible_spans if span.readability == "clear"]
    uncertain_spans = [span for span in compatible_spans if span.readability == "uncertain"]
    unreadable_spans = [span for span in compatible_spans if span.readability == "unreadable"]
    missing_pages = [page for page in roi_pages if page not in span_pages]
    semantic_compatible = _semantic_compatibility(question_job, compatible_spans)
    if incompatible_span_ids and not compatible_spans:
        semantic_compatible = False
    reference_structure = "\n".join(
        [
            str(question_job.answer_slice.problem or "") if question_job.answer_slice else "",
            str(question_job.answer_slice.reference_answer or "") if question_job.answer_slice else "",
        ]
    )
    expected_subparts = sorted(set(re.findall(r"\\textbf\{\((\d+)\)\}", reference_structure)))
    observed_text = "\n".join(span.text for span in compatible_spans)
    observed_subparts = sorted(set(re.findall(r"(?:^|\s)\((\d+)\)", observed_text)))
    subpart_coverage_complete = not expected_subparts or set(expected_subparts).issubset(observed_subparts)

    if question_job.route == "mismatch":
        status = EVIDENCE_MISMATCH
    elif not question_job.roi_refs:
        status = EVIDENCE_MISSING_ROUTE
    elif not spans:
        status = EVIDENCE_IMAGE_ONLY
    elif semantic_compatible is False:
        status = EVIDENCE_INCOMPLETE
    elif not subpart_coverage_complete:
        status = EVIDENCE_INCOMPLETE
    elif not clear_spans and unreadable_spans:
        status = EVIDENCE_INCOMPLETE
    elif missing_pages and len(roi_pages) > 1:
        status = EVIDENCE_INCOMPLETE
    else:
        status = EVIDENCE_READY

    critical_symbols = list(question_job.answer_slice.critical_symbols) if question_job.answer_slice else []
    symbol_names = {
        candidate.symbol
        for span in compatible_spans
        for candidate in span.symbol_candidates
        if candidate.symbol != "unknown"
    }
    negative_sign_risk = "-" in critical_symbols or "minus" in critical_symbols
    symbol_audit_required = negative_sign_risk and (
        bool(uncertain_spans)
        or "minus" in symbol_names
        or not clear_spans
    )

    return {
        "question_id": question_job.question_id,
        "status": status,
        "route": question_job.route,
        "source_pages": roi_pages,
        "transcription_pages": compatible_span_pages,
        "observed_transcription_pages": span_pages,
        "missing_pages": missing_pages,
        "roi_count": len(question_job.roi_refs),
        "span_count": len(compatible_spans),
        "observed_span_count": len(spans),
        "incompatible_span_ids": incompatible_span_ids,
        "clear_span_count": len(clear_spans),
        "uncertain_span_count": len(uncertain_spans),
        "unreadable_span_count": len(unreadable_spans),
        "all_subparts_found": status == EVIDENCE_READY,
        "semantic_compatible": semantic_compatible,
        "expected_subparts": expected_subparts,
        "observed_subparts": observed_subparts,
        "subpart_coverage_complete": subpart_coverage_complete,
        "ambiguous_route": any(ref.span_id.startswith("ambiguous-") for ref in question_job.roi_refs),
        "negative_sign_risk": negative_sign_risk,
        "symbol_audit_required": symbol_audit_required,
        "requires_rescue": status in {
            EVIDENCE_IMAGE_ONLY,
            EVIDENCE_INCOMPLETE,
            EVIDENCE_MISSING_ROUTE,
        }
        or semantic_compatible is False,
    }


def build_evidence_registry(
    jobs: dict[str, QuestionJob | dict[str, Any]],
    transcriptions: dict[str, list[TranscriptionSpan | dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    return {
        str(question_id): build_evidence_packet(
            job,
            transcriptions.get(str(question_id), []),
        )
        for question_id, job in jobs.items()
    }
