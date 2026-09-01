from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from typing import Any


VERDICTS = {"correct", "partial", "incorrect", "unreadable", "mismatch"}
DEDUCTION_STATUSES = {"partial", "incorrect"}
DETERMINISTIC_SYMBOLS = {"minus", "blank", "equals", "fraction_bar", "erasure"}
NEGATIVE_SYMBOLS = {"minus", "negative", "negative_sign", "负号", "负"}


def student_hash(student_id: str) -> str:
    return hashlib.sha256(str(student_id).encode("utf-8")).hexdigest()


def wilson_interval(successes: int, total: int, *, z: float = 1.96) -> dict[str, Any]:
    if total <= 0:
        return {"numerator": 0, "denominator": 0, "value": None, "wilson_95": [None, None]}
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return {
        "numerator": successes,
        "denominator": total,
        "value": p,
        "wilson_95": [max(0.0, center - margin), min(1.0, center + margin)],
    }


def _evidence_count(result: Mapping[str, Any]) -> int:
    refs = list(result.get("evidence_refs") or [])
    for decision in result.get("rubric_decisions") or []:
        refs.extend(decision.get("evidence_refs") or [])
    return sum(
        1
        for ref in refs
        if isinstance(ref, Mapping)
        and int(ref.get("page", 0) or 0) >= 1
        and isinstance(ref.get("bbox"), (list, tuple))
        and len(ref["bbox"]) == 4
    )


def _is_confirmed_gold(record: Mapping[str, Any]) -> bool:
    """Treat only explicit teacher-confirmed rows as production gold.

    Fixtures without an annotation field are useful for unit tests and are
    considered confirmed by convention.  A pending annotation status never
    contributes to a score, even if a stale expected value is present.
    """
    if record.get("teacher_confirmed") is True:
        return True
    status = record.get("annotation_status")
    if status is None:
        return True
    return str(status).strip().lower() in {"confirmed", "teacher_confirmed", "teacher-confirmed"}


def _candidate_map(
    candidates: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if isinstance(candidates, Mapping):
        return {str(key): value for key, value in candidates.items() if isinstance(value, Mapping)}
    result: dict[str, Mapping[str, Any]] = {}
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        key = item.get("student_hash")
        if not key and item.get("student_id"):
            key = student_hash(str(item["student_id"]))
        if key:
            result[str(key)] = item
    return result


def _question_records(gold_records: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        record
        for record in gold_records
        if _is_confirmed_gold(record)
        and str(record.get("question_id") or "")
        and str(record.get("expected_verdict") or "") in VERDICTS
    ]


def _model_question_records(records: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        record
        for record in records
        if str(record.get("annotation_source") or "") == "independent_multimodal_model_judge"
        and str(record.get("annotation_status") or "") == "model_confirmed"
        and record.get("scoreable") is True
        and str(record.get("question_id") or "")
        and str(record.get("expected_verdict") or "") in VERDICTS
    ]


def _question_result(candidate: Mapping[str, Any], question_id: str) -> Mapping[str, Any] | None:
    result = (candidate.get("question_results") or {}).get(question_id)
    return result if isinstance(result, Mapping) else None


def _sample_set(records: Iterable[Mapping[str, Any]], default: str = "teacher_confirmed_gold") -> str:
    splits = sorted({str(record.get("split")) for record in records if record.get("split")})
    return ",".join(splits) if splits else default


def _metric(successes: int, total: int, *, sample_set: str) -> dict[str, Any]:
    value = wilson_interval(successes, total)
    value["sample_set"] = sample_set
    value["status"] = "measured" if total else "unmeasured"
    return value


def _gold_rubrics(record: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(item.get("rubric_id")): str(item.get("status", "unknown"))
        for item in (record.get("rubric_decisions") or [])
        if isinstance(item, Mapping) and item.get("rubric_id")
    }


def _iter_candidate_deductions(candidate: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for question_id, result in (candidate.get("question_results") or {}).items():
        if not isinstance(result, Mapping):
            continue
        for decision in result.get("rubric_decisions") or []:
            if isinstance(decision, Mapping) and str(decision.get("status")) in DEDUCTION_STATUSES:
                yield str(question_id), decision


def _bbox_iou(left: Any, right: Any) -> float:
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)) or len(left) != 4 or len(right) != 4:
        return 0.0
    lx1, ly1, lx2, ly2 = (float(value) for value in left)
    rx1, ry1, rx2, ry2 = (float(value) for value in right)
    ix1, iy1, ix2, iy2 = max(lx1, rx1), max(ly1, ry1), min(lx2, rx2), min(ly2, ry2)
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    left_area = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1)
    right_area = max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1)
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def _candidate_spans(candidate: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for result in (candidate.get("question_results") or {}).values():
        if not isinstance(result, Mapping):
            continue
        for span in result.get("transcription") or []:
            if isinstance(span, Mapping):
                yield span


def _best_symbol(span: Mapping[str, Any]) -> tuple[str, float] | None:
    options = [item for item in (span.get("symbol_candidates") or []) if isinstance(item, Mapping)]
    options = [item for item in options if str(item.get("symbol")) in DETERMINISTIC_SYMBOLS]
    if not options:
        return None
    best = max(options, key=lambda item: float(item.get("confidence", 0) or 0))
    return str(best.get("symbol")), float(best.get("confidence", 0) or 0)


def _matching_span(candidate: Mapping[str, Any], record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    page = int(record.get("page", 0) or 0)
    bbox = record.get("bbox")
    matches = [
        span
        for span in _candidate_spans(candidate)
        if int(span.get("page", 0) or 0) == page and _bbox_iou(span.get("bbox"), bbox) >= 0.25
    ]
    return max(matches, key=lambda span: _bbox_iou(span.get("bbox"), bbox), default=None)


def _symbol_category(record: Mapping[str, Any]) -> str:
    return str(record.get("symbol_category") or "").strip().lower()


def _confirmed_symbol_records(symbol_records: Iterable[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
    if symbol_records is None:
        return []
    return [
        record
        for record in symbol_records
        if _is_confirmed_gold(record)
        and isinstance(record.get("bbox"), (list, tuple))
        and len(record["bbox"]) == 4
        and _symbol_category(record) not in {"", "pending_symbol_annotation", "unknown"}
    ]


def _usage_tokens(candidate: Mapping[str, Any]) -> int | None:
    usage = candidate.get("budget_usage") or {}
    if not isinstance(usage, Mapping) or not usage:
        audit = candidate.get("audit") or {}
        usage = audit.get("provider_usage") if isinstance(audit, Mapping) else {}
    if not isinstance(usage, Mapping):
        return None
    try:
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
    except (TypeError, ValueError):
        return None
    return input_tokens + output_tokens if input_tokens >= 0 and output_tokens >= 0 else None


def evaluate_candidates(
    gold_records: Iterable[Mapping[str, Any]],
    candidates: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    symbol_records: Iterable[Mapping[str, Any]] | None = None,
    legacy_usage: Mapping[str, Mapping[str, Any]] | None = None,
    run_records: Iterable[Mapping[str, Any]] | None = None,
    strong_model_names: Iterable[str] | None = None,
    model_judgments: Iterable[Mapping[str, Any]] | None = None,
    reference_source: str = "teacher",
) -> dict[str, Any]:
    """Evaluate candidate artifacts with auditable numerators and denominators.

    Optional symbol, legacy-usage, and run records are deliberately explicit:
    a missing side dataset produces an unmeasured metric instead of an inferred
    zero or a score based on incomplete evidence.
    """
    gold_records = list(gold_records)
    candidate_map = _candidate_map(candidates)
    if reference_source not in {"teacher", "model", "hybrid"}:
        raise ValueError("reference_source must be teacher, model, or hybrid")
    teacher_questions = _question_records(gold_records)
    model_rows = list(model_judgments or [])
    model_questions = _model_question_records(model_rows)
    if reference_source == "teacher":
        question_gold = teacher_questions
    elif reference_source == "model":
        question_gold = model_questions
    else:
        teacher_keys = {(str(row.get("student_hash")), str(row.get("question_id"))) for row in teacher_questions}
        question_gold = [*teacher_questions, *(row for row in model_questions if (str(row.get("student_hash")), str(row.get("question_id"))) not in teacher_keys)]
    default_sample_set = (
        "independent_model_judge"
        if reference_source == "model"
        else "teacher_and_independent_model_judge"
        if reference_source == "hybrid"
        else "teacher_confirmed_gold"
    )
    sample_set = _sample_set(question_gold, default=default_sample_set)

    question_total = question_match = 0
    no_evidence = 0
    overall_total = overall_match = 0
    coverage_total = coverage_match = 0
    for gold in question_gold:
        coverage_total += 1
        candidate = candidate_map.get(str(gold.get("student_hash", "")))
        if candidate is None:
            continue
        question_id = str(gold["question_id"])
        result = _question_result(candidate, question_id)
        coverage_match += int(result is not None)
        if result is None:
            continue
        expected = str(gold["expected_verdict"])
        question_total += 1
        question_match += int(str(result.get("verdict")) == expected)
        if str(result.get("verdict")) in DEDUCTION_STATUSES and _evidence_count(result) == 0:
            no_evidence += 1

    overall_gold: dict[str, tuple[Mapping[str, Any], str]] = {}
    for gold in gold_records:
        if not _is_confirmed_gold(gold) or not gold.get("expected_overall") or not gold.get("student_hash"):
            continue
        overall_gold.setdefault(str(gold["student_hash"]), (gold, str(gold["expected_overall"])))
    for student, (gold, expected_overall) in overall_gold.items():
        candidate = candidate_map.get(student)
        if candidate is None:
            continue
        overall_total += 1
        overall_match += int(str(candidate.get("overall")) == expected_overall)

    # False-positive deductions are only meaningful against confirmed teacher
    # rubric rows.  ``question_gold`` is already filtered for scoring, but the
    # explicit guard here prevents pending rows from silently becoming a
    # denominator when callers pass a mixed gold file.
    gold_by_question = {
        (str(gold.get("student_hash")), str(gold.get("question_id"))): gold
        for gold in question_gold
        if _is_confirmed_gold(gold)
    }
    false_positive_total = false_positive = 0
    for student, candidate in candidate_map.items():
        for question_id, decision in _iter_candidate_deductions(candidate):
            gold = gold_by_question.get((student, question_id))
            if gold is None:
                continue
            false_positive_total += 1
            gold_status = _gold_rubrics(gold).get(str(decision.get("rubric_id")), "correct")
            false_positive += int(gold_status not in DEDUCTION_STATUSES)

    symbol_gold = _confirmed_symbol_records(symbol_records)
    negative_gold = [record for record in symbol_gold if _symbol_category(record) in NEGATIVE_SYMBOLS and str(record.get("readability", "")).lower() not in {"unreadable", "unknown"}]
    negative_match = 0
    symbol_predictions = symbol_correct = 0
    for record in symbol_gold:
        candidate = candidate_map.get(str(record.get("student_hash", "")))
        span = _matching_span(candidate, record) if candidate is not None else None
        prediction = _best_symbol(span) if span is not None else None
        if record in negative_gold:
            negative_match += int(prediction is not None and prediction[0] == "minus")
        if prediction is not None and prediction[1] >= 0.9:
            symbol_predictions += 1
            expected_symbol = "minus" if _symbol_category(record) in NEGATIVE_SYMBOLS else _symbol_category(record)
            symbol_correct += int(prediction[0] == expected_symbol)

    token_total = token_new = token_old = 0
    token_ratios: list[float] = []
    if legacy_usage:
        for student, candidate in candidate_map.items():
            new_tokens = _usage_tokens(candidate)
            old_usage = legacy_usage.get(student)
            if new_tokens is None or not isinstance(old_usage, Mapping):
                continue
            try:
                old_tokens = int(old_usage.get("input_tokens", 0) or 0) + int(old_usage.get("output_tokens", 0) or 0)
            except (TypeError, ValueError):
                continue
            if old_tokens <= 0:
                continue
            token_total += 1
            token_new += new_tokens
            token_old += old_tokens
            token_ratios.append(new_tokens / old_tokens)

    run_values = list(run_records) if run_records is not None else []
    graph_failed = sum(
        1
        for record in run_values
        if isinstance(record, Mapping) and not bool(record.get("candidate_available", record.get("candidate")))
    )
    p95_token_ratio = None
    if token_ratios:
        ordered_ratios = sorted(token_ratios)
        p95_token_ratio = ordered_ratios[max(0, math.ceil(len(ordered_ratios) * 0.95) - 1)]

    strong_names = {str(name) for name in strong_model_names or () if str(name)}
    strong_total = strong_count = 0
    if strong_model_names is not None:
        for candidate in candidate_map.values():
            audit = candidate.get("audit") or {}
            model = audit.get("model") if isinstance(audit, Mapping) else None
            if model:
                strong_total += 1
                strong_count += int(str(model) in strong_names)

    model_disputed = sum(1 for row in model_rows if str(row.get("annotation_status")) == "model_disputed")
    model_total = sum(1 for row in model_rows if str(row.get("annotation_source")) == "independent_multimodal_model_judge")
    candidate_supported = sum(1 for row in model_questions if row.get("candidate_supported") is True)

    return {
        "question_verdict_accuracy": _metric(question_match, question_total, sample_set=sample_set),
        "overall_accuracy": _metric(overall_match, overall_total, sample_set="teacher_confirmed_student_overall"),
        "error_accusation_false_positive_rate": _metric(false_positive, false_positive_total, sample_set="teacher_confirmed_rubric_deductions"),
        "severe_misjudgment_rate": _metric(overall_total - overall_match, overall_total, sample_set="teacher_confirmed_student_overall"),
        "question_coverage_recall": _metric(coverage_match, coverage_total, sample_set=sample_set),
        "negative_sign_recall": _metric(negative_match, len(negative_gold), sample_set="teacher_confirmed_symbol_hard_set"),
        "critical_symbol_precision": _metric(symbol_correct, symbol_predictions, sample_set="teacher_confirmed_symbol_hard_set"),
        "no_evidence_deductions": _metric(no_evidence, question_total, sample_set=sample_set),
        "average_token_ratio": {
            "numerator": token_new if token_total else 0,
            "denominator": token_old if token_total else 0,
            "value": token_new / token_old if token_old else None,
            "sample_set": "matched_student_runtime_usage",
            "matched_students": token_total,
            "status": "measured" if token_total else "unmeasured",
        },
        "p95_token_ratio": {
            "numerator": p95_token_ratio,
            "denominator": token_total,
            "value": p95_token_ratio,
            "sample_set": "matched_student_runtime_usage",
            "matched_students": token_total,
            "status": "measured" if token_total else "unmeasured",
        },
        "strong_model_trigger_rate": _metric(strong_count, strong_total, sample_set="candidate_audit_records"),
        "graph_failure_rate": _metric(graph_failed, len(run_values), sample_set="candidate_run_records"),
        "model_judge_dispute_rate": _metric(model_disputed, model_total, sample_set="independent_multimodal_model_judge"),
        "model_judge_candidate_support_rate": _metric(candidate_supported, len(model_questions), sample_set="model_confirmed_questions"),
        "reference_source": reference_source,
        "gold_records": len(gold_records),
        "teacher_confirmed_question_records": len(teacher_questions),
        "model_confirmed_question_records": len(model_questions),
        "scored_question_records": len(question_gold),
    }
