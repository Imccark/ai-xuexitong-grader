from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.grading_graph.budget import BudgetedJsonProvider, BudgetLedger
from app.grading_graph.cache import CachedJsonProvider, JsonResponseCache
from app.grading_graph.nodes.evidence_gate import build_evidence_registry
from app.grading_graph.nodes.image_quality import (
    RECTIFICATION_VERSION,
    apply_multimodal_orientation_correction,
    materialize_image_variants,
)
from app.grading_graph.nodes.local_layout import (
    LocalLayoutBackend,
    LocalLayoutObserver,
    LocalLayoutSettings,
    LocalLayoutUnavailable,
    QuestionLabelReader,
)
from app.grading_graph.nodes.page_observer import PageObserver
from app.grading_graph.nodes.page_router import build_question_jobs
from app.grading_graph.nodes.transcriber import LiteralTranscriber
from app.grading_graph.schemas import AnswerManifest, EvidenceRef, PageArtifact, FileRef, Budget, TranscriptionSpan
from app.grading_graph.store import file_sha256


def _student_hash(student_id: str) -> str:
    return hashlib.sha256(student_id.encode("utf-8")).hexdigest()


def _file_ref(path: Path) -> FileRef:
    media_type = "image/png" if path.suffix.lower() == ".png" else None
    return FileRef(path=str(path.resolve()), sha256=file_sha256(path), media_type=media_type)


def _page_number(path: Path) -> int:
    stem = path.stem.lower()
    if not stem.startswith("page_"):
        raise ValueError(f"unexpected processed page name: {path.name}")
    return int(stem.removeprefix("page_"))


def _center(bbox: tuple[int, int, int, int]) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)


def _question_for_span(span: TranscriptionSpan, observations: list[dict[str, Any]]) -> str | None:
    x, y = _center(span.bbox)
    best_id: str | None = None
    best_overlap = 0
    best_distance = float("inf")
    for question in observations:
        bbox = tuple(int(value) for value in question.get("bbox", (0, 0, 0, 0)))
        if bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]:
            return str(question.get("question_id"))
        sx1, sy1, sx2, sy2 = span.bbox
        overlap = max(0, min(sx2, bbox[2]) - max(sx1, bbox[0])) * max(0, min(sy2, bbox[3]) - max(sy1, bbox[1]))
        qx, qy = _center(bbox)
        distance = (x - qx) ** 2 + (y - qy) ** 2
        if overlap > best_overlap or (overlap == best_overlap and distance < best_distance):
            best_id, best_overlap, best_distance = str(question.get("question_id")), overlap, distance
    return best_id


def _observation_indices_for_span(
    span: TranscriptionSpan,
    observations: list[dict[str, Any]],
    *,
    min_overlap_ratio: float = 0.1,
) -> list[int]:
    """Return every materially overlapping ROI, not only the center-most one.

    Qwen often emits one transcription span covering two adjacent subquestions.
    Assigning it only by center silently drops evidence for the first ROI.  A
    small overlap threshold keeps header/edge noise from duplicating into every
    nearby question while preserving broad spans that contain multiple ROIs.
    """
    sx1, sy1, sx2, sy2 = span.bbox
    span_area = max(1, (sx2 - sx1) * (sy2 - sy1))
    matches: list[tuple[int, int]] = []
    for index, question in enumerate(observations):
        bbox = tuple(int(value) for value in question.get("bbox", (0, 0, 0, 0)))
        q_area = max(1, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
        overlap = max(0, min(sx2, bbox[2]) - max(sx1, bbox[0])) * max(
            0, min(sy2, bbox[3]) - max(sy1, bbox[1])
        )
        if overlap and overlap / min(span_area, q_area) >= min_overlap_ratio:
            matches.append((index, overlap))
    if matches:
        return [index for index, _ in matches]
    fallback = _question_for_span(span, observations)
    if fallback is None:
        return []
    return [index for index, question in enumerate(observations) if str(question.get("question_id")) == fallback]


def _question_marker_present(text: str, question_id: str) -> bool:
    """Accept common OCR variants such as ``1.15`` for ``1.1.5``."""

    prefix = str(text).strip()[:32]
    literal = str(question_id).strip().lower()
    if literal and literal in prefix.lower():
        return True
    expected_digits = "".join(re.findall(r"\d+", literal))
    observed_digits = "".join(re.findall(r"\d+", prefix))
    return len(expected_digits) >= 2 and observed_digits.startswith(expected_digits)


def _agent_page_dimensions(page: PageArtifact) -> tuple[int, int]:
    """Return dimensions in the coordinate system sent to the vision agent."""

    rectified = page.quality.get("rectified_quality")
    source = rectified if isinstance(rectified, dict) else page.quality
    width = max(1, int(source.get("width", page.quality.get("width", 1)) or 1))
    height = max(1, int(source.get("height", page.quality.get("height", 1)) or 1))
    return width, height


def _expand_ambiguous_shared_routes(
    question_jobs: dict[str, Any],
    observation_job_ids: dict[tuple[int, int], list[str]],
    pages: list[PageArtifact],
) -> list[dict[str, Any]]:
    """Give a locator full-page context when one generic ROI maps to siblings.

    OCR frequently sees only the unsuffixed heading ``1.1.1`` and the router
    conservatively attaches that ROI to both ``(1)`` and ``(2)``.  Such a
    shared ROI is not enough to grade either subpart.  Mark it explicitly and
    add a bounded all-page view for the answer-blind locator.
    """

    events: list[dict[str, Any]] = []
    page_refs: list[EvidenceRef] = []
    for page in pages:
        if page.page_type == "blank" or page.normalized is None:
            continue
        width, height = _agent_page_dimensions(page)
        page_refs.append(
            EvidenceRef(
                span_id=f"ambiguous-fallback-page-{page.page}",
                page=page.page,
                bbox=(0, 0, width, height),
                artifact_ref=page.normalized.path,
                view="normalized",
            )
        )
        if len(page_refs) >= 4:
            break
    for (page_number, observation_index), mapped_ids in observation_job_ids.items():
        unique_ids = sorted(set(mapped_ids))
        if len(unique_ids) < 2:
            continue
        shared_id = f"p{page_number}-q{observation_index}"
        for question_id in unique_ids:
            job = question_jobs.get(question_id)
            if job is None:
                continue
            marked_refs = [
                ref.model_copy(update={"span_id": f"ambiguous-{ref.span_id}"})
                if ref.span_id == shared_id
                else ref
                for ref in job.roi_refs
            ]
            existing_pages = {ref.page for ref in marked_refs}
            expanded_refs = [*marked_refs]
            for ref in page_refs:
                if ref.page not in existing_pages:
                    expanded_refs.append(ref)
                    existing_pages.add(ref.page)
            question_jobs[question_id] = job.model_copy(
                update={
                    "pages": sorted(existing_pages),
                    "roi_refs": expanded_refs,
                    "route": "risk",
                }
            )
            events.append(
                {
                    "stage": "ambiguous_shared_route",
                    "page": page_number,
                    "span_id": shared_id,
                    "question_id": question_id,
                }
            )
    return events


def _reassign_cross_page_continuations(
    question_jobs: dict[str, Any],
    transcriptions: dict[str, list[dict[str, Any]]],
    normalized_by_page: dict[int, str],
) -> list[dict[str, Any]]:
    """Attach page-top writing before the first heading to the prior page job.

    Handwritten work frequently continues onto the next page without repeating
    the question number.  The page observer then assigns those first lines to
    the next visible heading.  We correct only the unambiguous layout case:
    the prior ROI touches the bottom edge, a math-bearing span sits wholly
    above the next page's first recognized question heading, and that span
    contains no current-page question marker.
    """

    events: list[dict[str, Any]] = []
    all_pages = sorted(
        {
            int(ref.page)
            for job in question_jobs.values()
            for ref in getattr(job, "roi_refs", [])
        }
    )
    for page in all_pages:
        previous_page = page - 1
        if previous_page not in all_pages or page not in normalized_by_page:
            continue
        current_ids = {
            question_id
            for question_id, job in question_jobs.items()
            if any(ref.page == page for ref in job.roi_refs)
        }
        unique_spans: dict[str, TranscriptionSpan] = {}
        owners: dict[str, set[str]] = {}
        for question_id in current_ids:
            for raw in transcriptions.get(question_id, []):
                try:
                    span = TranscriptionSpan.model_validate(raw)
                except ValueError:
                    continue
                if span.page != page:
                    continue
                unique_spans.setdefault(span.span_id, span)
                owners.setdefault(span.span_id, set()).add(question_id)
        heading_spans = [
            span
            for span in unique_spans.values()
            if any(_question_marker_present(span.text, question_id) for question_id in current_ids)
        ]
        if not heading_spans:
            continue
        first_heading_y = min(span.bbox[1] for span in heading_spans)
        candidates = [
            span
            for span in unique_spans.values()
            if span.bbox[3] <= first_heading_y
            and len(span.text.strip()) >= 8
            and re.search(r"(?:=|≠|≤|≥|\d|解|矩阵|秩)", span.text)
            and not any(_question_marker_present(span.text, question_id) for question_id in current_ids)
        ]
        if not candidates:
            continue
        previous_candidates = [
            (question_id, job, ref)
            for question_id, job in question_jobs.items()
            for ref in job.roi_refs
            if ref.page == previous_page
        ]
        if not previous_candidates:
            continue
        page_bottom = max(1000, max(ref.bbox[3] for _, _, ref in previous_candidates))
        previous_id, previous_job, previous_ref = max(
            previous_candidates,
            key=lambda value: value[2].bbox[3],
        )
        if previous_ref.bbox[3] < page_bottom * 0.85:
            continue
        for span in sorted(candidates, key=lambda value: value.bbox[1]):
            raw_span = span.model_dump(mode="json")
            for owner in owners.get(span.span_id, set()):
                transcriptions[owner] = [
                    raw
                    for raw in transcriptions.get(owner, [])
                    if str(raw.get("span_id")) != span.span_id
                ]
            if not any(raw.get("span_id") == span.span_id for raw in transcriptions.get(previous_id, [])):
                transcriptions.setdefault(previous_id, []).append(raw_span)
            continuation_ref = EvidenceRef(
                span_id=span.span_id,
                page=page,
                bbox=span.bbox,
                artifact_ref=normalized_by_page[page],
                view="normalized",
            )
            if not any(ref.span_id == span.span_id for ref in previous_job.roi_refs):
                previous_job = previous_job.model_copy(
                    update={
                        "pages": sorted(set(previous_job.pages) | {page}),
                        "roi_refs": [*previous_job.roi_refs, continuation_ref],
                        "route": "risk",
                    }
                )
                question_jobs[previous_id] = previous_job
            events.append(
                {
                    "stage": "cross_page_continuation",
                    "page": page,
                    "span_id": span.span_id,
                    "question_id": previous_id,
                }
            )
    return events


def _answer_texts(manifest: AnswerManifest, manifest_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    root = manifest_path.resolve().parent
    for question_id, answer_slice in manifest.questions.items():
        path = (root / answer_slice.artifact_ref).resolve()
        if root not in path.parents or not path.is_file():
            continue
        values[question_id] = path.read_text(encoding="utf-8")
    return values


def _load_previous_question_fallback(
    artifact_root: Path,
    student_id: str,
) -> tuple[dict[int, dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    """Recover same-image page/line evidence when a transient observer call fails.

    Candidate artifacts are content-addressed by student hash and are written
    only after a graph run completes.  A previous successful version therefore
    provides a safe local fallback for a transient page-observer timeout.  We
    use it only to preserve routing/transcription coverage; the current run
    still performs grading and verification against the current manifest.
    """
    root = artifact_root / "agent_artifacts" / _student_hash(student_id)
    input_manifest = root / "input_manifest.json"
    try:
        previous_manifest = json.loads(input_manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    if previous_manifest.get("preprocess_version") != RECTIFICATION_VERSION:
        return {}, {}
    files: list[Path] = []
    current = root / "question_reviews.json"
    if current.is_file():
        files.append(current)
    versions = root / "candidate_versions"
    if versions.is_dir():
        files.extend(sorted(versions.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True))
    selected: dict[str, dict[str, Any]] = {}
    observations: dict[int, dict[str, Any]] = {}
    evidence_file = root / "page_evidence.json"
    if evidence_file.is_file():
        try:
            evidence_payload = json.loads(evidence_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            evidence_payload = {}
        for raw_observation in evidence_payload.get("observations", []) if isinstance(evidence_payload, dict) else []:
            if not isinstance(raw_observation, dict):
                continue
            try:
                page = int(raw_observation.get("page"))
            except (TypeError, ValueError):
                continue
            questions = raw_observation.get("questions") if isinstance(raw_observation.get("questions"), list) else []
            observations[page] = {
                "page": page,
                "page_type": str(raw_observation.get("page_type") or "unknown"),
                "questions": [dict(question) for question in questions if isinstance(question, dict)],
            }
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        question_results = payload.get("question_results") if isinstance(payload, dict) else None
        if not isinstance(question_results, dict):
            continue
        for question_id, result in question_results.items():
            if str(question_id) in selected or not isinstance(result, dict):
                continue
            refs = result.get("evidence_refs") if isinstance(result.get("evidence_refs"), list) else []
            spans = result.get("transcription") if isinstance(result.get("transcription"), list) else []
            if refs or spans:
                selected[str(question_id)] = {"refs": refs, "spans": spans}

    spans_by_page: dict[int, list[dict[str, Any]]] = {}
    for question_id, item in selected.items():
        refs = item["refs"]
        spans = item["spans"]
        page_items: dict[int, list[tuple[int, int, int, int]]] = {}
        for raw_ref in refs:
            if not isinstance(raw_ref, dict):
                continue
            try:
                page = int(raw_ref.get("page"))
                bbox = tuple(int(value) for value in raw_ref.get("bbox", []))
                if page < 1 or len(bbox) != 4 or min(bbox) < 0 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                    continue
            except (TypeError, ValueError):
                continue
            page_items.setdefault(page, []).append(bbox)
        for raw_span in spans:
            if not isinstance(raw_span, dict):
                continue
            try:
                page = int(raw_span.get("page"))
                bbox = tuple(int(value) for value in raw_span.get("bbox", []))
                if page < 1 or len(bbox) != 4 or min(bbox) < 0 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                    continue
                TranscriptionSpan.model_validate(raw_span)
            except (TypeError, ValueError):
                continue
            spans_by_page.setdefault(page, []).append(raw_span)
            page_items.setdefault(page, []).append(bbox)
        for page, bboxes in page_items.items():
            x1 = min(bbox[0] for bbox in bboxes)
            y1 = min(bbox[1] for bbox in bboxes)
            x2 = max(bbox[2] for bbox in bboxes)
            y2 = max(bbox[3] for bbox in bboxes)
            item = {
                "question_id": question_id,
                "bbox": [x1, y1, x2, y2],
                "confidence": 0.5,
                "question_type": "unknown",
            }
            page_observation = observations.setdefault(page, {"page": page, "page_type": "assignment", "questions": []})
            if not any(str(existing.get("question_id")) == question_id for existing in page_observation["questions"]):
                page_observation["questions"].append(item)
    return observations, spans_by_page


def build_student_graph_input(
    *,
    processed_student_dir: Path | str,
    answer_manifest_path: Path | str,
    artifact_root: Path | str,
    provider: Any,
    assignment_id: str,
    student_id: str,
    run_id: str,
    budget: Budget,
    graph_version: str = "langgraph-v3-evidence-first",
    cache: JsonResponseCache | None = None,
    budget_ledger: BudgetLedger | None = None,
    local_layout_config: dict[str, Any] | None = None,
    local_layout_backend: LocalLayoutBackend | None = None,
    question_label_reader: QuestionLabelReader | None = None,
) -> dict[str, Any]:
    """Prepare image evidence, page observations, and question state for Graph.

    This is candidate-only preparation: it writes only under ``agent_artifacts``
    and leaves processed images and formal results untouched.
    """
    processed_dir = Path(processed_student_dir).resolve()
    manifest_path = Path(answer_manifest_path).resolve()
    artifact_root = Path(artifact_root).resolve()
    manifest = AnswerManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    page_paths = sorted(processed_dir.glob("page_*.png"), key=_page_number)
    if not page_paths:
        raise FileNotFoundError(f"no processed pages found: {processed_dir}")
    if manifest.assignment_id != assignment_id:
        raise ValueError("answer manifest assignment_id does not match graph assignment_id")

    prep_provider: Any = provider
    if budget_ledger is not None:
        prep_provider = BudgetedJsonProvider(prep_provider, budget_ledger)
    if cache is not None:
        # Cache hits must bypass the paid-call/token ledger.
        prep_provider = CachedJsonProvider(prep_provider, cache)
    observer = PageObserver(prep_provider)
    transcriber = LiteralTranscriber(prep_provider)
    local_layout_settings = LocalLayoutSettings.from_mapping(
        local_layout_config,
        base_dir=Path(__file__).resolve().parents[1],
    )
    local_observer = (
        LocalLayoutObserver(
            local_layout_settings,
            manifest,
            backend=local_layout_backend,
            label_reader=question_label_reader,
        )
        if local_layout_settings.enabled
        else None
    )
    previous_observations, previous_spans_by_page = _load_previous_question_fallback(artifact_root, student_id)
    pages: list[PageArtifact] = []
    observations: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    layout_audit: list[dict[str, Any]] = []
    recovered_pages: set[int] = set()
    all_student_artifacts = artifact_root / "agent_artifacts" / _student_hash(student_id) / "pages"

    for page_path in page_paths:
        page = _page_number(page_path)
        data = page_path.read_bytes()
        variants_dir = all_student_artifacts / f"page_{page}"
        variants = materialize_image_variants(
            data,
            variants_dir,
            max_pixels=budget.max_image_pixels or 120_000_000,
        )
        quality = json.loads(Path(variants["quality"]).read_text(encoding="utf-8"))
        pages.append(
            PageArtifact(
                page=page,
                original=_file_ref(variants["original"]),
                rectified=_file_ref(variants["rectified"]),
                normalized=_file_ref(variants["normalized"]),
                enhanced=_file_ref(variants["enhanced"]),
                quality=quality,
                page_type="blank" if quality.get("is_near_blank") else "unknown",
            )
        )
        if quality.get("is_near_blank"):
            observations.append({"page": page, "page_type": "blank", "questions": []})
            continue
        observation: dict[str, Any] | None = None
        if local_observer is not None:
            try:
                local_result = local_observer.observe(variants["normalized"], page=page)
                layout_audit.append(dict(local_result["audit"]))
                if bool(local_result.get("accepted")):
                    observation = dict(local_result["observation"])
                    warnings.append(
                        {
                            "stage": "local_layout_accepted",
                            "page": page,
                            "model_name": local_layout_settings.model_name,
                            "engine": local_layout_settings.engine,
                            "question_count": len(observation.get("questions", [])),
                        }
                    )
                else:
                    warnings.append(
                        {
                            "stage": "local_layout_online_fallback",
                            "page": page,
                            "reasons": list(local_result.get("audit", {}).get("reasons", [])),
                        }
                    )
            except LocalLayoutUnavailable as exc:
                layout_audit.append(
                    {
                        "source": "local_layout",
                        "status": "unavailable",
                        "page": page,
                        "model_name": local_layout_settings.model_name,
                        "engine": local_layout_settings.engine,
                        "error_type": type(exc).__name__,
                    }
                )
                warnings.append(
                    {
                        "stage": "local_layout_unavailable_online_fallback",
                        "page": page,
                        "error_type": type(exc).__name__,
                    }
                )
            except Exception as exc:
                layout_audit.append(
                    {
                        "source": "local_layout",
                        "status": "failed",
                        "page": page,
                        "model_name": local_layout_settings.model_name,
                        "engine": local_layout_settings.engine,
                        "error_type": type(exc).__name__,
                    }
                )
                warnings.append(
                    {
                        "stage": "local_layout_failed_online_fallback",
                        "page": page,
                        "error_type": type(exc).__name__,
                    }
                )
        try:
            if observation is None:
                observation = observer.observe(variants["normalized"], page=page)
            fallback_rotation = int(observation.get("rotation_degrees_clockwise", 0) or 0)
            fallback_confidence = float(observation.get("orientation_confidence", 0.0) or 0.0)
            if fallback_rotation in {90, 180, 270} and fallback_confidence >= 0.90:
                quality = apply_multimodal_orientation_correction(
                    variants,
                    rotation_degrees_clockwise=fallback_rotation,
                    confidence=fallback_confidence,
                )
                pages[-1] = pages[-1].model_copy(
                    update={
                        "rectified": _file_ref(variants["rectified"]),
                        "normalized": _file_ref(variants["normalized"]),
                        "enhanced": _file_ref(variants["enhanced"]),
                        "quality": quality,
                    }
                )
                warnings.append(
                    {
                        "stage": "orientation_page_observer_fallback",
                        "page": page,
                        "rotation_degrees_clockwise": fallback_rotation,
                        "confidence": round(fallback_confidence, 6),
                    }
                )
                observation = observer.observe(variants["normalized"], page=page)
        except Exception as exc:
            observation = previous_observations.get(page)
            if observation and observation.get("questions"):
                recovered_pages.add(page)
                warnings.append(
                    {
                        "stage": "page_observer_recovered",
                        "page": page,
                        "source": "same_image_previous_candidate_artifact",
                        "error_type": type(exc).__name__,
                    }
                )
            else:
                observation = {"page": page, "page_type": "unknown", "questions": []}
                # The manifest-driven fallback below will create bounded
                # full-page jobs for every unrouted question.  Treat this as
                # a recoverable observation warning rather than a hard
                # candidate error: the question grader still sees the source
                # image and can complete the work.
                warnings.append(
                    {
                        "stage": "page_observer_full_page_rescue",
                        "page": page,
                        "error_type": type(exc).__name__,
                    }
                )
        observations.append(observation)

    routed = build_question_jobs(observations, manifest)
    question_jobs = dict(routed["question_jobs"])
    # Replace any model-provided artifact reference with the materialized local
    # normalized page.  The observer may identify a region, but it cannot choose
    # which filesystem path a later audit node opens.
    normalized_by_page = {
        page.page: page.normalized.path
        for page in pages
        if page.normalized is not None
    }
    for question_id, job in list(question_jobs.items()):
        question_jobs[question_id] = job.model_copy(
            update={
                "roi_refs": [
                    ref.model_copy(
                        update={
                            "artifact_ref": normalized_by_page.get(ref.page, ref.artifact_ref),
                            "view": "normalized",
                        }
                    )
                    for ref in job.roi_refs
                ]
            }
        )
    observation_job_ids: dict[tuple[int, int], list[str]] = {}
    for question_id, job in question_jobs.items():
        for ref in job.roi_refs:
            match = re.match(r"^p(\d+)-q(\d+)$", ref.span_id)
            if match:
                key = (int(match.group(1)), int(match.group(2)))
                observation_job_ids.setdefault(key, []).append(question_id)
    warnings.extend(_expand_ambiguous_shared_routes(question_jobs, observation_job_ids, pages))
    for question_id, answer_slice in manifest.questions.items():
        if question_id not in question_jobs:
            from app.grading_graph.schemas import QuestionJob

            # A missed heading is a router failure, not proof that the
            # student's answer is unreadable.  Give the rescue grader a
            # bounded whole-page view (at most four non-blank pages) so it can
            # locate the requested question from the visible question id.
            # This branch is used only when normal ROI routing found nothing,
            # so the extra image cost is paid for abstentions rather than for
            # every question.
            fallback_refs: list[EvidenceRef] = []
            fallback_pages: list[int] = []
            for page_artifact in pages:
                if page_artifact.page_type == "blank" or page_artifact.normalized is None:
                    continue
                width, height = _agent_page_dimensions(page_artifact)
                fallback_pages.append(page_artifact.page)
                fallback_refs.append(
                    EvidenceRef(
                        span_id=f"fallback-page-{page_artifact.page}",
                        page=page_artifact.page,
                        bbox=(0, 0, width, height),
                        artifact_ref=page_artifact.normalized.path,
                        view="normalized",
                    )
                )
                if len(fallback_refs) >= 4:
                    break
            question_jobs[question_id] = QuestionJob(
                question_id=question_id,
                pages=fallback_pages,
                roi_refs=fallback_refs,
                answer_slice=answer_slice,
                question_type=answer_slice.question_type,
                route="risk" if fallback_refs else "unreadable",
            )
    if routed["mismatch_question_ids"]:
        errors.append({"stage": "page_router", "error_type": "reference_mismatch", "question_ids": routed["mismatch_question_ids"]})

    transcriptions: dict[str, list[dict[str, Any]]] = {question_id: [] for question_id in question_jobs}
    for observation in observations:
        page = int(observation["page"])
        question_observations = observation.get("questions", [])
        if not question_observations:
            continue
        page_path = next(path for path in page_paths if _page_number(path) == page)
        normalized_page_path = Path(normalized_by_page.get(page, str(page_path)))
        roi_refs = [
            EvidenceRef(
                span_id=f"p{page}-q{index + 1}",
                page=page,
                bbox=tuple(int(value) for value in question["bbox"]),
                artifact_ref=str(normalized_page_path),
                view="normalized",
            )
            for index, question in enumerate(question_observations)
        ]
        if page in recovered_pages and previous_spans_by_page.get(page):
            spans = [TranscriptionSpan.model_validate(value) for value in previous_spans_by_page[page]]
        else:
            try:
                spans = transcriber.transcribe(
                    normalized_page_path,
                    page=page,
                    roi_refs=roi_refs,
                )
            except Exception as exc:
                fallback_spans = previous_spans_by_page.get(page, [])
                if fallback_spans:
                    spans = [TranscriptionSpan.model_validate(value) for value in fallback_spans]
                    warnings.append(
                        {
                            "stage": "transcriber_recovered",
                            "page": page,
                            "source": "same_image_previous_candidate_artifact",
                            "error_type": type(exc).__name__,
                        }
                    )
                else:
                    errors.append({"stage": "transcriber", "page": page, "error_type": type(exc).__name__})
                    for question in question_observations:
                        question_id = str(question.get("question_id") or "")
                        if question_id in question_jobs:
                            question_jobs[question_id] = question_jobs[question_id].model_copy(update={"route": "unreadable"})
                    continue
        for span in spans:
            for index in _observation_indices_for_span(span, question_observations):
                question_id = question_observations[index].get("question_id")
                mapped_ids = observation_job_ids.get((page, index + 1), [])
                target_ids = mapped_ids or ([str(question_id)] if question_id else [])
                for target_id in target_ids:
                    if target_id in transcriptions:
                        # A broad span may overlap multiple adjacent ROIs, and
                        # duplicate observations can point to the same routed
                        # question. Keep one copy per span_id so symbol-audit
                        # and verifier budgets are not consumed twice.
                        if not any(item.get("span_id") == span.span_id for item in transcriptions[target_id]):
                            transcriptions[target_id].append(span.model_dump(mode="json"))

    warnings.extend(
        _reassign_cross_page_continuations(
            question_jobs,
            transcriptions,
            normalized_by_page,
        )
    )

    serialized_jobs = {key: value.model_dump(mode="json") for key, value in question_jobs.items()}
    evidence_registry = build_evidence_registry(serialized_jobs, transcriptions)
    return {
        "schema_version": "1.0",
        "graph_version": graph_version,
        "preprocess_version": RECTIFICATION_VERSION,
        "run_id": run_id,
        "assignment_id": assignment_id,
        "student_id": student_id,
        "answer_manifest": manifest.model_dump(mode="json"),
        "pages": [page.model_dump(mode="json") for page in pages],
        "page_observations": observations,
        "local_layout": local_layout_settings.audit_dict(),
        "layout_audit": layout_audit,
        "question_jobs": serialized_jobs,
        "transcriptions": transcriptions,
        "evidence_registry": evidence_registry,
        "answer_texts": _answer_texts(manifest, manifest_path),
        "budget": budget.model_dump(mode="json"),
        "errors": errors,
        "warnings": warnings,
    }


def load_compiled_manifest(path: Path | str) -> AnswerManifest:
    return AnswerManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))
