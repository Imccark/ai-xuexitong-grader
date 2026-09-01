from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable

from app.grading_graph.schemas import AnswerManifest, AnswerSliceRef
from app.grading_graph.store import atomic_write_bytes, atomic_write_json, file_sha256


QUESTION_ID_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+){1,3})(?:\s*[（(]\s*(\d+)\s*[）)])?")
REFERENCE_COMPILER_VERSION = "reference-compiler-v4"
HEADING_RE = re.compile(
    r"(?m)^\s*(?P<macro>\\(?:section|subsection|subsubsection|paragraph))\*?\{(?P<label>[^}\n]*)\}"
)
PROBLEM_RE = re.compile(r"(?m)^\s*\\problem\{(?P<label>[^}\n]*)\}")
PROBLEM_ENV_RE = re.compile(r"(?m)^\s*\\begin\{problem\}\{(?P<label>[^}\n]*)\}")
ANSWER_HEADING_RE = re.compile(
    r"(?:\\(?:section|subsection|subsubsection|paragraph)\*?\{(?:答案|解答|检查)[^}\n]*\}|\\begin\{solution\})",
    re.IGNORECASE,
)
QUESTION_WORDS = ("题目", "练习", "习题", "problem", "exercise")


def _normalize_question_id(match: re.Match[str]) -> str:
    base = match.group(1)
    sub = match.group(2)
    return f"{base} ({sub})" if sub else base


def _question_id(label: str) -> str | None:
    match = QUESTION_ID_RE.search(label)
    if not match:
        return None
    return _normalize_question_id(match)


def _is_question_heading(macro: str, label: str) -> bool:
    lowered = label.lower()
    if macro == r"\problem":
        return True
    if any(word in lowered for word in QUESTION_WORDS):
        return True
    return macro in {r"\subsection", r"\subsubsection", r"\paragraph"} and bool(_question_id(label))


def _question_type(block: str) -> str:
    if re.search(r"证明|证明题|prove|proof", block, re.IGNORECASE):
        return "proof"
    if re.search(r"计算|求|det|rank|特征值|对角化", block, re.IGNORECASE):
        return "calculation"
    return "other"


def _checks(block: str) -> list[str]:
    checks: list[str] = []
    lowered = block.lower()
    if "det" in lowered or "行列式" in block:
        checks.append("determinant")
    if "rank" in lowered or "秩" in block:
        checks.append("rank")
    if "trace" in lowered or "迹" in block:
        checks.append("trace")
    if "\u03bb" in block or "lambda" in lowered or "特征值" in block:
        checks.append("eigenvalue")
    return checks


def _aliases(label: str, question_id: str) -> list[str]:
    """Emit safe formatting aliases; reviewed old-version aliases remain editable."""
    values: list[str] = []
    normalized_label = " ".join(label.split())
    if normalized_label and normalized_label != question_id:
        values.append(normalized_label)
    compact_subquestion = question_id.replace(" (", "(")
    if compact_subquestion != question_id:
        values.append(compact_subquestion)
    return list(dict.fromkeys(values))


def parse_question_blocks(source: str) -> list[dict[str, Any]]:
    matches: list[tuple[int, int, str, str]] = []
    for match in HEADING_RE.finditer(source):
        macro = match.group("macro")
        label = match.group("label").strip()
        if _is_question_heading(macro, label) and _question_id(label):
            matches.append((match.start(), match.end(), macro, label))
    for match in PROBLEM_RE.finditer(source):
        label = match.group("label").strip()
        if _question_id(label):
            matches.append((match.start(), match.end(), r"\problem", label))
    for match in PROBLEM_ENV_RE.finditer(source):
        label = match.group("label").strip()
        if _question_id(label):
            matches.append((match.start(), match.end(), r"\problem", label))
    matches.sort(key=lambda item: item[0])
    blocks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, (start, heading_end, macro, label) in enumerate(matches):
        question_id = _question_id(label)
        if not question_id or question_id in seen:
            continue
        seen.add(question_id)
        end = matches[index + 1][0] if index + 1 < len(matches) else len(source)
        raw_block = source[start:end].strip()
        answer_match = ANSWER_HEADING_RE.search(raw_block)
        reference_answer = raw_block[answer_match.start():].strip() if answer_match else raw_block
        problem_text = raw_block[: answer_match.start()].strip() if answer_match else raw_block
        rubric_text = re.sub(r"\s+", " ", problem_text).strip()
        blocks.append(
            {
                "question_id": question_id,
                "heading": label,
                "question_type": _question_type(raw_block),
                "problem": problem_text,
                "reference_answer": reference_answer,
                "rubric_items": [
                    {
                        "id": "r1",
                        "requirement": rubric_text[:500] or "按标准答案核对本题结论与推导。",
                    }
                ],
                "critical_symbols": sorted(set(re.findall(r"[-=]|\\lambda|\\mu|\\mathsf\{T\}|\\det", raw_block))),
                "deterministic_checks": _checks(raw_block),
                "aliases": _aliases(label, question_id),
                "source_range": [start, end],
            }
        )
    return blocks


def compile_reference_answer(
    tex_path: Path | str,
    *,
    cache_dir: Path | str,
    assignment_id: str,
    compiler_version: str = REFERENCE_COMPILER_VERSION,
) -> AnswerManifest:
    tex_path = Path(tex_path).resolve()
    cache_dir = Path(cache_dir).resolve()
    source = tex_path.read_text(encoding="utf-8")
    answer_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    cache_path = cache_dir / f"{answer_hash}.json"
    if cache_path.is_file():
        cached = AnswerManifest.model_validate_json(cache_path.read_text(encoding="utf-8"))
        if (
            cached.assignment_id == assignment_id
            and cached.compiler_version == compiler_version
            and (cached.questions or cached.reference_status == "ready")
        ):
            return cached

    blocks = parse_question_blocks(source)
    slices: dict[str, AnswerSliceRef] = {}
    slice_dir = cache_dir / "reference_slices"
    for block in blocks:
        text = str(block["reference_answer"])
        slice_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        slice_path = slice_dir / f"{slice_hash}.tex"
        atomic_write_bytes(slice_path, text.encode("utf-8"))
        slices[block["question_id"]] = AnswerSliceRef(
            question_id=block["question_id"],
            artifact_ref=str(slice_path.relative_to(cache_dir)).replace("\\", "/"),
            sha256=slice_hash,
            character_count=len(text),
            heading=str(block["heading"]),
            aliases=[str(value) for value in block.get("aliases", [])],
            question_type=str(block["question_type"]),
            problem=str(block["problem"]),
            reference_answer=text,
            rubric_items=[
                {
                    "id": str(item["id"]),
                    "requirement": str(item["requirement"]),
                }
                for item in block.get("rubric_items", [])
            ],
            critical_symbols=[str(value) for value in block.get("critical_symbols", [])],
            deterministic_checks=[str(value) for value in block.get("deterministic_checks", [])],
            source_range=tuple(int(value) for value in block["source_range"]),
        )
    manifest = AnswerManifest(
        assignment_id=assignment_id,
        answer_hash=answer_hash,
        compiler_version=compiler_version,
        questions=slices,
        reference_status="ready" if slices else "needs_review",
    )
    atomic_write_json(cache_path, manifest.model_dump(mode="json"))
    return manifest


def match_observed_questions(
    observed_question_ids: Iterable[str],
    manifest_question_ids: Iterable[str] | Mapping[str, Any] | AnswerManifest,
) -> dict[str, Any]:
    alias_to_canonical: dict[str, str] = {}
    if isinstance(manifest_question_ids, AnswerManifest):
        items = manifest_question_ids.questions.items()
    elif isinstance(manifest_question_ids, Mapping):
        items = manifest_question_ids.items()
    else:
        items = ((str(value), None) for value in manifest_question_ids)
    for canonical, reference in items:
        canonical_id = " ".join(str(canonical).split())
        if not canonical_id:
            continue
        aliases = getattr(reference, "aliases", None)
        if aliases is None and isinstance(reference, Mapping):
            aliases = reference.get("aliases", [])
        for value in [canonical_id, *(aliases or [])]:
            normalized = " ".join(str(value).split())
            if normalized:
                alias_to_canonical[normalized] = canonical_id
    matched: list[str] = []
    unmatched: list[str] = []
    for raw in observed_question_ids:
        question_id = " ".join(str(raw).split())
        canonical_id = alias_to_canonical.get(question_id)
        if canonical_id is not None and canonical_id not in matched:
            matched.append(canonical_id)
        elif question_id not in unmatched:
            unmatched.append(question_id)
    return {
        "status": "reference_mismatch" if unmatched else "ready",
        "matched": matched,
        "unmatched": unmatched,
    }
