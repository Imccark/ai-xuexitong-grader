from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw

from evaluation.model_judge import candidate_snapshot_hash

from grading_graph.store import atomic_write_json


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def candidate_paths(root: Path) -> list[Path]:
    return sorted(root.glob("**/agent_artifacts/*/candidate_result.json"))


def _evidence_pages(result: dict[str, Any]) -> list[int]:
    pages: list[int] = []
    for ref in result.get("evidence_refs") or []:
        if isinstance(ref, dict) and int(ref.get("page", 0) or 0) >= 1:
            pages.append(int(ref["page"]))
    for span in result.get("transcription") or []:
        if isinstance(span, dict) and int(span.get("page", 0) or 0) >= 1:
            pages.append(int(span["page"]))
    # A compound answer may span more than two photographed pages.  Keep a
    # bounded but complete evidence set for the blind judge; truncating here
    # can manufacture ``disputed`` rows even when the candidate has clear
    # transcription on a later page.
    return list(dict.fromkeys(pages))[:4]


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


_STUDENT_ID_RE = re.compile(r"(?<!\d)\d{8,20}(?!\d)")
_NAME_AND_ID_RE = re.compile(r"[\u4e00-\u9fff]{2,6}\s*(?:[:：]?\s*)\d{8,20}")
_LABELED_NAME_RE = re.compile(r"(?:姓名|名字|学生)\s*[:：]?\s*[\u4e00-\u9fff]{2,6}")


def _contains_pii(text: str) -> bool:
    value = str(text or "")
    return bool(
        _STUDENT_ID_RE.search(value)
        or _NAME_AND_ID_RE.search(value)
        or _LABELED_NAME_RE.search(value)
    )


def _redact_transcription(spans: Any) -> list[dict[str, Any]]:
    """Remove OCR-visible names/IDs before writing the blind context."""
    if not isinstance(spans, list):
        return []
    output: list[dict[str, Any]] = []
    for raw in spans:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        text = str(item.get("text", ""))
        text = _LABELED_NAME_RE.sub("[REDACTED_STUDENT]", text)
        text = _NAME_AND_ID_RE.sub("[REDACTED_STUDENT]", text)
        text = _STUDENT_ID_RE.sub("[REDACTED_STUDENT_ID]", text)
        item["text"] = text
        output.append(item)
    return output


def _anonymize_image(source: Path, destination: Path, spans: list[dict[str, Any]]) -> None:
    """Create a judge-only image with detected identity spans painted out."""
    with Image.open(source) as image:
        image = image.convert("RGB")
        draw = ImageDraw.Draw(image)
        width, height = image.size
        for span in spans:
            bbox = span.get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            try:
                x1, y1, x2, y2 = (float(value) for value in bbox)
            except (TypeError, ValueError):
                continue
            # Most graph bboxes use a 0..1000 coordinate space; scale them to
            # the source image before applying a small privacy padding.
            if max(abs(x1), abs(x2)) <= 1000 and width > 1000:
                x1, x2 = x1 * width / 1000, x2 * width / 1000
            if max(abs(y1), abs(y2)) <= 1000 and height > 1000:
                y1, y2 = y1 * height / 1000, y2 * height / 1000
            pad_x = max(4, (x2 - x1) * 0.08)
            pad_y = max(4, (y2 - y1) * 0.15)
            draw.rectangle(
                (max(0, int(x1 - pad_x)), max(0, int(y1 - pad_y)), min(width, int(x2 + pad_x)), min(height, int(y2 + pad_y))),
                fill="white",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, format="PNG")


def _page_variants(
    candidate_path: Path,
    result: dict[str, Any],
    root: Path,
    *,
    packet_dir: Path | None = None,
    extra_pages: list[int] | None = None,
    preferred_pages: list[int] | None = None,
) -> list[dict[str, Any]]:
    pages_root = candidate_path.parent / "pages"
    page_numbers = list(preferred_pages or _evidence_pages(result))
    fallback_all_pages = False
    if not page_numbers and extra_pages:
        # For unreadable slices, page_evidence may still identify the actual
        # handwritten page (for example a compound exercise split into (1)/(2)).
        page_numbers = list(extra_pages)
    for page in extra_pages or []:
        if page not in page_numbers:
            page_numbers.append(page)
    if not page_numbers and pages_root.is_dir():
        # An unreadable candidate may have lost all question/page routing
        # evidence even though the student's processed pages are intact.  A
        # bounded all-page fallback gives the blind judge a chance to locate
        # the answer (especially for handwritten headings) instead of
        # manufacturing a dispute solely because the router abstained.
        fallback_all_pages = True
        for page_root in sorted(pages_root.glob("page_*"))[:8]:
            try:
                page_numbers.append(int(page_root.name.removeprefix("page_")))
            except ValueError:
                continue
    pages: list[dict[str, Any]] = []
    page_limit = 8 if fallback_all_pages else 4
    for page_number in page_numbers[:page_limit]:
        page_root = pages_root / f"page_{page_number}"
        page_spans = [
            span
            for span in (result.get("transcription") or [])
            if isinstance(span, dict)
            and int(span.get("page", 0) or 0) == page_number
            and _contains_pii(str(span.get("text", "")))
        ]
        variants: dict[str, str] = {}
        for name in ("original.png", "rectified.png", "normalized.png", "enhanced.png"):
            source = page_root / name
            if not source.is_file():
                continue
            if page_spans and packet_dir is not None:
                anonymized = packet_dir / f"anonymized_page_{page_number}_{name}"
                _anonymize_image(source, anonymized, page_spans)
                variants[name.removesuffix(".png")] = _relative(anonymized, root)
            else:
                variants[name.removesuffix(".png")] = _relative(source, root)
        if variants:
            pages.append({"page": page_number, "variants": variants})
    return pages


def _normalise_question_id(value: Any) -> str:
    """Normalize common OCR variants without guessing an unresolved prefix.

    In particular, OCR frequently drops the second dot in ``1.2.1`` and
    emits full-width parentheses.  Bare ``(1)`` remains a suffix marker and
    is deliberately not assigned to a numbered question globally.
    """
    text = " ".join(str(value or "").replace("（", "(").replace("）", ")").split())
    text = re.sub(r"\s*\.\s*", ".", text)
    text = re.sub(r"\s*\(\s*", " (", text)
    text = re.sub(r"\s*\)\s*", ")", text)
    text = re.sub(r"^(\d+)\.(\d)(\d)(\s*\(\d+\))$", r"\1.\2.\3\4", text)
    return text.strip()


def _question_id_matches(observed: Any, target: str) -> bool:
    observed_norm = _normalise_question_id(observed)
    target_norm = _normalise_question_id(target)
    if observed_norm == target_norm:
        return True
    # A reference may represent a compound exercise as ``1.2.1`` while OCR
    # emits its visible subpart labels ``1.2.1 (1)``/``(2)``.
    if not re.search(r"\(\d+\)\s*$", target_norm) and observed_norm.startswith(target_norm + " ("):
        return True
    # OCR commonly drops the opening parenthesis and emits ``1)``/``2)`` or
    # prefixes a suffix as ``1(1)``.  Treat these as suffix markers only when
    # the target question has the same explicit subpart.
    suffix_match = re.fullmatch(r"(?:.*?\()?\s*(\d+)\)", observed_norm)
    target_suffix = re.search(r"\((\d+)\)\s*$", target_norm)
    if suffix_match and target_suffix and suffix_match.group(1) == target_suffix.group(1):
        return True
    # A shortened hierarchical label such as ``1.2`` is a safe page hint for
    # its manifest children (``1.2.1``/``1.2.2``).  This affects only evidence
    # page recovery; it never changes the candidate verdict or routing.
    return bool(
        re.fullmatch(r"\d+(?:\.\d+)+", observed_norm)
        and target_norm.startswith(observed_norm + ".")
    )


def _page_evidence_pages(candidate_path: Path, question_id: str) -> list[int]:
    """Recover pages when a broad/incorrect candidate ref hid a sub-question."""
    evidence_path = candidate_path.parent / "page_evidence.json"
    if not evidence_path.is_file():
        return []
    try:
        evidence = read_json(evidence_path)
    except (OSError, ValueError):
        return []
    suffix_match = re.search(r"\((\d+)\)\s*$", str(question_id))
    suffix = suffix_match.group(1) if suffix_match else None
    pages: list[int] = []
    for observation in evidence.get("observations") or []:
        if not isinstance(observation, dict):
            continue
        page = int(observation.get("page", 0) or 0)
        for question in observation.get("questions") or []:
            observed = question.get("question_id", "")
            if _question_id_matches(observed, str(question_id)):
                if page >= 1 and page not in pages:
                    pages.append(page)
    return pages


def _compound_rescue_pages(
    candidate_path: Path,
    result: dict[str, Any],
    reference: dict[str, Any],
) -> list[int]:
    """Keep bounded all-page evidence for compound-question rescue runs."""

    outcomes = {
        str(item.get("outcome") or "")
        for item in (result.get("attempt_history") or [])
        if isinstance(item, dict)
    }
    used_compound_rescue = bool(
        outcomes
        & {
            "full_page_subpart_rescue",
            "full_page_subpart_rescue_provider_error",
            "located_with_full_page_context",
        }
    )
    problem = str(reference.get("problem") or "")
    rubrics = reference.get("rubric_items") or []
    explicit_subparts = set(re.findall(r"\\textbf\{\((\d+)\)\}", problem))
    rubric_subparts = {
        str(item.get("id") or item.get("rubric_id") or "")
        for item in rubrics
        if isinstance(item, dict)
        and str(item.get("id") or item.get("rubric_id") or "").startswith("subpart_")
    }
    if not used_compound_rescue or (len(explicit_subparts) < 2 and len(rubric_subparts) < 2):
        return []
    pages_root = candidate_path.parent / "pages"
    pages: list[int] = []
    for page_root in sorted(pages_root.glob("page_*"), key=lambda path: path.name):
        try:
            page = int(page_root.name.removeprefix("page_"))
        except ValueError:
            continue
        if any((page_root / name).is_file() for name in ("original.png", "rectified.png", "normalized.png", "enhanced.png")):
            pages.append(page)
        if len(pages) >= 4:
            break
    return pages


def _page_text_index(candidate_path: Path) -> dict[int, str]:
    """Collect all page-level OCR text, not just the current question slice."""
    result_path = candidate_path
    try:
        candidate = read_json(result_path)
    except (OSError, ValueError):
        return {}
    page_text: dict[int, list[str]] = {}
    for raw_result in (candidate.get("question_results") or {}).values():
        if not isinstance(raw_result, dict):
            continue
        for span in raw_result.get("transcription") or []:
            if not isinstance(span, dict):
                continue
            page = int(span.get("page", 0) or 0)
            text = str(span.get("text", "") or "").strip()
            if page >= 1 and text:
                page_text.setdefault(page, []).append(text)
    return {page: "\n".join(parts) for page, parts in page_text.items()}


def _reference_terms(reference: dict[str, Any]) -> set[str]:
    source = " ".join(
        str(reference.get(key, "") or "")
        for key in ("heading", "problem", "reference_answer", "rubric_items")
    )
    # Keep meaningful Chinese words, identifiers and Greek symbols while
    # ignoring the huge amount of matrix punctuation/numeric noise.
    terms = set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}|[α-ωΑ-Ω]", source))
    output = {term.lower() for term in terms if term.lower() not in {"subsection", "textbf", "begin", "end"}}
    # OCR returns Greek glyphs while LaTeX references contain names.
    output.update({"β"} if "beta" in output else set())
    output.update({"α"} if "alpha" in output else set())
    output.update({"γ"} if "gamma" in output else set())
    return output


def _recover_question_pages(
    candidate_path: Path,
    result: dict[str, Any],
    reference: dict[str, Any],
    question_id: str,
    evidence_pages: list[int],
) -> list[int]:
    """Rank pages by question-specific OCR evidence and retain safe fallbacks.

    Candidate ROI refs are still retained, but a clearly stronger page-level
    match can replace a parser-misrouted ref.  This prevents blind judges from
    seeing a neighbouring exercise while never inventing a page when no OCR
    evidence exists.
    """
    page_text = _page_text_index(candidate_path)
    if not page_text:
        return []
    terms = _reference_terms(reference)
    if not terms:
        return []
    target_norm = _normalise_question_id(question_id)
    observations: dict[int, set[str]] = {}
    evidence_path = candidate_path.parent / "page_evidence.json"
    if evidence_path.is_file():
        try:
            evidence = read_json(evidence_path)
        except (OSError, ValueError):
            evidence = {}
        for observation in evidence.get("observations") or []:
            if not isinstance(observation, dict):
                continue
            page = int(observation.get("page", 0) or 0)
            if page < 1:
                continue
            for question in observation.get("questions") or []:
                if isinstance(question, dict):
                    observations.setdefault(page, set()).add(_normalise_question_id(question.get("question_id", "")))
    scored: list[tuple[float, int]] = []
    for page, text in page_text.items():
        page_terms = {term.lower() for term in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}|[α-ωΑ-Ω]", text)}
        page_terms.update({"β"} if "beta" in page_terms else set())
        page_terms.update({"α"} if "alpha" in page_terms else set())
        page_terms.update({"γ"} if "gamma" in page_terms else set())
        overlap = len(terms & page_terms)
        # OCR labels are useful hints, but a mistaken exact label must not
        # outweigh stronger answer-text evidence from another page.
        label_bonus = 1 if target_norm in observations.get(page, set()) else 0
        suffix_bonus = 0
        if overlap or label_bonus or suffix_bonus:
            scored.append((overlap + label_bonus + suffix_bonus, page))
    if not scored:
        return []
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score = scored[0][0]
    # Do not replace a candidate ref with a weak, generic page match.
    if best_score < 2:
        return []
    ranked = [page for score, page in scored if score >= max(2, best_score - 1)]
    return ranked[:4]


def _packet_id(assignment_id: str, student_hash: str, question_id: str) -> str:
    raw = f"{assignment_id}\0{student_hash}\0{question_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def prepare_packets(
    *,
    root: Path,
    manifest_root: Path,
    output_root: Path,
    max_students: int,
    max_questions: int,
    overwrite: bool = False,
) -> dict[str, Any]:
    if max_students <= 0 or max_questions <= 0:
        raise ValueError("max_students and max_questions must be positive")
    root = root.resolve()
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_cache: dict[str, dict[str, Any]] = {}
    students: set[tuple[str, str]] = set()
    packet_rows: list[dict[str, Any]] = []

    for candidate_path in candidate_paths(root):
        candidate = read_json(candidate_path)
        assignment_id = str(candidate.get("assignment_id") or "")
        student_hash = candidate_path.parent.name
        student_key = (assignment_id, student_hash)
        if student_key not in students and len(students) >= max_students:
            continue
        if assignment_id not in manifest_cache:
            manifest_path = manifest_root.resolve() / assignment_id / "manifest.json"
            if not manifest_path.is_file():
                continue
            manifest_cache[assignment_id] = read_json(manifest_path)
        references = manifest_cache[assignment_id].get("questions") or {}

        for question_id, raw_result in (candidate.get("question_results") or {}).items():
            if len(packet_rows) >= max_questions:
                break
            if not isinstance(raw_result, dict):
                continue
            reference = references.get(str(question_id))
            if not isinstance(reference, dict):
                continue
            packet_id = _packet_id(assignment_id, student_hash, str(question_id))
            packet_dir = output_root / packet_id
            blind_path = packet_dir / "blind_context.json"
            candidate_context_path = packet_dir / "candidate_context.json"
            if packet_dir.exists() and not overwrite:
                packet_rows.append({
                    "packet_id": packet_id,
                    "assignment_id": assignment_id,
                    "student_hash": student_hash,
                    "question_id": str(question_id),
                    "status": "existing",
                    "blind_context": _relative(blind_path, root),
                    "candidate_context": _relative(candidate_context_path, root),
                })
                students.add(student_key)
                continue
            packet_dir.mkdir(parents=True, exist_ok=True)
            evidence_pages = _page_evidence_pages(candidate_path, str(question_id))
            compound_rescue_pages = _compound_rescue_pages(candidate_path, raw_result, reference)
            # Preserve every page already attached to the candidate result as
            # well as router-recovered pages.  The ranked reference-term
            # heuristic is only an ordering hint and must never discard a
            # concrete transcription/evidence page.
            evidence_pages = list(
                dict.fromkeys([*_evidence_pages(raw_result), *evidence_pages, *compound_rescue_pages])
            )[:4]
            preferred_pages = _recover_question_pages(
                candidate_path,
                raw_result,
                reference,
                str(question_id),
                evidence_pages,
            )
            selected_extra_pages = evidence_pages
            pages = _page_variants(
                candidate_path,
                raw_result,
                root,
                packet_dir=packet_dir,
                extra_pages=selected_extra_pages,
                preferred_pages=preferred_pages or None,
            )
            blind = {
                "schema_version": "1.0",
                "packet_id": packet_id,
                "assignment_id": assignment_id,
                "question_id": str(question_id),
                "reference": {
                    "question_type": reference.get("question_type", "unknown"),
                    "problem": reference.get("problem", ""),
                    "reference_answer": reference.get("reference_answer", ""),
                    "rubric_items": reference.get("rubric_items") or [],
                    "critical_symbols": reference.get("critical_symbols") or [],
                },
                "machine_transcription_untrusted": _redact_transcription(raw_result.get("transcription") or []),
                "image_pages": pages,
                "blindness_contract": "candidate verdict and grading rationale are intentionally absent",
            }
            candidate_view = {
                "schema_version": "1.0",
                "packet_id": packet_id,
                "candidate": {
                    key: raw_result.get(key)
                    for key in (
                        "verdict",
                        "confidence",
                        "evidence_refs",
                        "rubric_decisions",
                        "needs_verification",
                        "verifier_result",
                        "risk_flags",
                    )
                },
                "candidate_snapshot_hash": candidate_snapshot_hash(raw_result),
            }
            atomic_write_json(blind_path, blind)
            atomic_write_json(candidate_context_path, candidate_view)
            packet_rows.append({
                "packet_id": packet_id,
                "assignment_id": assignment_id,
                "student_hash": student_hash,
                "question_id": str(question_id),
                "status": "pending_blind_judgment",
                "blind_context": _relative(blind_path, root),
                "candidate_context": _relative(candidate_context_path, root),
            })
            students.add(student_key)
        if len(packet_rows) >= max_questions:
            break

    index_path = output_root / "index.jsonl"
    index_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in packet_rows),
        encoding="utf-8",
    )
    pii_in_transcription = False
    for path in output_root.glob("*/blind_context.json"):
        if not path.is_file():
            continue
        packet = read_json(path)
        pii_in_transcription = pii_in_transcription or any(
            _contains_pii(str(span.get("text", "")))
            for span in (packet.get("machine_transcription_untrusted") or [])
            if isinstance(span, dict)
        )
    return {
        "packet_count": len(packet_rows),
        "student_count": len(students),
        "output_root": str(output_root),
        "index": str(index_path),
        "contains_student_names": pii_in_transcription,
        "requires_api_key": False,
    }


def iter_index(path: Path) -> Iterable[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                yield value
