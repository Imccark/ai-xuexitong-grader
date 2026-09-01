from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, Iterable

from app.grading_graph.schemas import AnswerManifest, EvidenceRef, QuestionJob


def _resolve_observed_ids(observed: str, answer_manifest: AnswerManifest, aliases: dict[str, str]) -> list[str]:
    """Resolve common OCR punctuation/segment omissions to a manifest ID."""
    normalized = " ".join(str(observed).split())
    direct = aliases.get(normalized)
    if direct:
        return [direct]
    compact_digits = re.sub(r"\D", "", normalized)
    digit_matches = [
        canonical_id
        for canonical_id in answer_manifest.questions
        if re.sub(r"\D", "", canonical_id) == compact_digits
    ]
    if len(digit_matches) == 1:
        return [digit_matches[0]]
    tail = normalized.rstrip(".").split(".")[-1]
    if tail.isdigit():
        tail_matches = []
        for canonical_id in answer_manifest.questions:
            base_id = re.sub(r"\s*\(\d+\)$", "", canonical_id).rstrip(".")
            if base_id.split(".")[-1] == tail:
                tail_matches.append(canonical_id)
        if len(tail_matches) == 1:
            return [tail_matches[0]]
    # Models sometimes omit the middle section in sub-question labels, e.g.
    # ``1.1 (1)`` for the manifest's ``1.1.1 (1)``.
    match = re.match(r"^(.*?)\s*\((\d+)\)$", normalized)
    if match:
        base, suffix = match.groups()
        base = re.sub(r"\s+", "", base).rstrip(".")
        if base in answer_manifest.questions:
            # The manifest may keep an example's sub-parts in one combined
            # answer slice (for example ``1.2.1``), while OCR emits ``(1)``.
            return [base]
        candidates = []
        for canonical_id in answer_manifest.questions:
            c_match = re.match(r"^(.*?)\s*\((\d+)\)$", canonical_id)
            if c_match and c_match.group(2) == suffix:
                canonical_base = re.sub(r"\s+", "", c_match.group(1))
                if canonical_base.startswith(base + "."):
                    candidates.append(canonical_id)
        if len(candidates) == 1:
            return [candidates[0]]
    # If OCR emits the unsuffixed base of a manifest group (for example
    # ``1.1.1`` while the manifest stores ``1.1.1 (1)/(2)``), reuse the same
    # observed ROI for every uniquely matching sub-question.  This preserves
    # evidence instead of manufacturing a reference mismatch.
    base_matches = []
    for canonical_id in answer_manifest.questions:
        c_match = re.match(r"^(.*?)\s*\((\d+)\)$", canonical_id)
        if c_match and re.sub(r"\s+", "", c_match.group(1)) == re.sub(r"\s+", "", normalized):
            base_matches.append(canonical_id)
    if len(base_matches) > 1:
        return base_matches
    return [normalized]


def _resolve_observed_id(observed: str, answer_manifest: AnswerManifest, aliases: dict[str, str]) -> str:
    return _resolve_observed_ids(observed, answer_manifest, aliases)[0]


def _is_plausible_question_id(value: str) -> bool:
    """Ignore OCR list markers/noise while still flagging real ID mismatches."""
    return bool(re.fullmatch(r"\d+(?:\.\d+){2,}(?:\s*\(\d+\))?", value))


def build_question_jobs(
    page_observations: Iterable[dict[str, Any]],
    answer_manifest: AnswerManifest,
    *,
    confidence_threshold: float = 0.8,
) -> dict[str, Any]:
    jobs: dict[str, QuestionJob] = {}
    confidence_by_question: dict[str, list[float]] = defaultdict(list)
    mismatch_ids: list[str] = []
    alias_to_question_id: dict[str, str] = {}
    for canonical_id, answer_slice in answer_manifest.questions.items():
        for alias in [canonical_id, *answer_slice.aliases]:
            normalized = " ".join(str(alias).split())
            if normalized:
                alias_to_question_id[normalized] = canonical_id
    for observation in page_observations:
        page = int(observation.get("page", 0))
        if page < 1 or observation.get("page_type") in {"cover", "blank", "wrong_subject"}:
            continue
        for index, question in enumerate(observation.get("questions", [])):
            observed_question_id = " ".join(str(question.get("question_id", "")).split())
            if not observed_question_id:
                continue
            question_ids = _resolve_observed_ids(observed_question_id, answer_manifest, alias_to_question_id)
            confidence = float(question.get("confidence", 0))
            bbox = tuple(int(value) for value in question.get("bbox", (0, 0, 1, 1)))
            artifact_ref = str(question.get("artifact_ref") or f"page_{page}.png")
            evidence = EvidenceRef(
                span_id=f"p{page}-q{index + 1}",
                page=page,
                bbox=bbox,
                artifact_ref=artifact_ref,
                view="original",
            )
            for question_id in question_ids:
                answer_slice = answer_manifest.questions.get(question_id)
                route = "fast"
                if answer_slice is None:
                    if not _is_plausible_question_id(question_id):
                        continue
                    route = "mismatch"
                    if question_id not in mismatch_ids:
                        mismatch_ids.append(question_id)
                confidence_by_question[question_id].append(confidence)
                if question_id not in jobs:
                    jobs[question_id] = QuestionJob(
                        question_id=question_id,
                        pages=[page],
                        roi_refs=[evidence],
                        answer_slice=answer_slice,
                        question_type=str(
                            question.get("question_type")
                            or (answer_slice.question_type if answer_slice else "unknown")
                        ),
                        route=route,
                    )
                else:
                    job = jobs[question_id]
                    if page not in job.pages:
                        job.pages.append(page)
                    job.roi_refs.append(evidence)
                    if route == "mismatch":
                        job.route = "mismatch"

    for question_id, job in jobs.items():
        if job.route != "mismatch" and any(value < confidence_threshold for value in confidence_by_question[question_id]):
            job.route = "risk"
    return {
        "status": "reference_mismatch" if mismatch_ids else "ready",
        "question_jobs": jobs,
        "mismatch_question_ids": mismatch_ids,
    }
