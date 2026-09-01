from __future__ import annotations

import re
from typing import Any

from app.grading_graph.schemas import QuestionJob, RubricDecision


def _clean_tex(value: str) -> str:
    return " ".join(str(value).replace("\n", " ").split())


def _split_latex_items(answer_text: str) -> list[str]:
    parts = re.split(r"\\item(?:\[[^\]]*\])?\s*", answer_text)
    return [_clean_tex(part) for part in parts[1:] if _clean_tex(part)]


def _split_numbered_subparts(answer_text: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"\\textbf\{\((\d+)\)\}")
    matches = list(pattern.finditer(answer_text))
    output: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(answer_text)
        requirement = _clean_tex(answer_text[match.end():end])
        if requirement:
            output.append((match.group(1), requirement))
    return output


def compile_atomic_rubrics(
    job: QuestionJob | dict[str, Any],
    answer_text: str,
) -> list[dict[str, str]]:
    """Compile bounded, deterministic rubric atoms from one answer slice.

    This is intentionally conservative: it only splits explicit LaTeX list
    items/sub-parts and proof bounds.  If the source has no reliable structure,
    the compiler preserves the manifest rubric instead of inventing criteria.
    """

    question_job = QuestionJob.model_validate(job)
    existing = list(question_job.answer_slice.rubric_items) if question_job.answer_slice else []
    atoms: list[dict[str, str]] = []

    subparts = _split_numbered_subparts(answer_text)
    if len(subparts) >= 2:
        atoms.extend(
            {
                "id": f"subpart_{number}",
                "requirement": requirement[:1800],
            }
            for number, requirement in subparts
        )
    else:
        list_items = _split_latex_items(answer_text)
        if len(list_items) >= 2:
            atoms.extend(
                {
                    "id": f"branch_{index}",
                    "requirement": requirement[:1800],
                }
                for index, requirement in enumerate(list_items, 1)
            )

    normalized_type = str(question_job.question_type or "").lower()
    proof_text = _clean_tex(answer_text)
    # RREF exercises often bundle three independently scoreable skills into
    # one long reference block.  Keeping them as one rubric makes a single
    # missed minus sign erase otherwise valid elimination work and correct
    # pivot/free-column conclusions.  Split only when the reference explicitly
    # names all of these concepts; this avoids inventing criteria for generic
    # matrix calculations.
    is_proof = normalized_type == "proof" or "证明" in proof_text
    if (
        not is_proof
        and ("rref" in answer_text.lower() or "行最简形" in answer_text or "行变换" in answer_text)
        and "主列" in answer_text
        and "自由列" in answer_text
    ):
        # The reference solution often shows row operations even when the
        # question asks only for the RREF and pivot/free columns.  A reference
        # method is not automatically a scoring requirement; requiring those
        # mechanical steps caused correct concise answers to lose credit.
        atoms = [
            {"id": "final_rref", "requirement": "最终行最简形矩阵（RREF）的每个元素与符号正确"},
            {"id": "pivot_free_columns", "requirement": "主列与自由列判断正确"},
        ]
    if is_proof:
        proof_atoms: list[dict[str, str]] = []
        is_rank_augmentation_proof = (
            "rank" in answer_text.lower()
            and ("A'" in answer_text or "A^{" in answer_text or "增广" in answer_text)
        )
        if is_rank_augmentation_proof:
            proof_atoms = [
                {"id": "proof_lower_bound", "requirement": "说明增广矩阵的秩不小于系数矩阵的秩，即 r' ≥ r"},
                {"id": "proof_one_column_bound", "requirement": "说明只增加一列至多增加一个主元，因此 r' ≤ r+1"},
                {"id": "proof_conclusion", "requirement": "推出 0 ≤ r'-r ≤ 1"},
            ]
        elif re.search(r"r\s*['′]\s*(?:\\geq|\\geqslant|≥)\s*r", answer_text):
            proof_atoms.append({"id": "proof_lower_bound", "requirement": "说明增广矩阵的秩不小于系数矩阵的秩，即 r' ≥ r"})
        if re.search(r"r\s*['′]\s*(?:\\leq|\\leqslant|≤)\s*r\s*\+\s*1", answer_text):
            proof_atoms.append({"id": "proof_one_column_bound", "requirement": "说明只增加一列至多增加一个主元，因此 r' ≤ r+1"})
        if re.search(r"0\s*(?:\\leq|\\leqslant|≤).*?r\s*['′]\s*-\s*r.*?(?:\\leq|\\leqslant|≤)\s*1", answer_text):
            proof_atoms.append({"id": "proof_conclusion", "requirement": "推出 0 ≤ r'-r ≤ 1"})
        if len(proof_atoms) >= 2:
            # Regex augmentation may rediscover an atom already emitted by
            # the rank-proof template. Preserve stable order but never ask
            # the model to score the same criterion twice.
            deduplicated: list[dict[str, str]] = []
            seen_ids: set[str] = set()
            for atom in proof_atoms:
                if atom["id"] in seen_ids:
                    continue
                deduplicated.append(atom)
                seen_ids.add(atom["id"])
            atoms = deduplicated

    if not atoms:
        atoms = [
            {
                "id": str(item.get("id") or item.get("rubric_id") or f"r{index + 1}"),
                "requirement": str(item.get("requirement") or item.get("description") or "")[:1800],
            }
            for index, item in enumerate(existing)
            if isinstance(item, dict)
        ]
    if not atoms and answer_text.strip():
        atoms = [{"id": "r1", "requirement": _clean_tex(answer_text)[:1800]}]
    return atoms[:8]


def deterministic_rubric_verdict(
    decisions: list[RubricDecision | dict[str, Any]],
    expected_rubric_ids: list[str],
) -> str | None:
    """Return a verdict only when every atomic rubric has a decisive status."""

    normalized = [RubricDecision.model_validate(item) for item in decisions]
    by_id = {item.rubric_id: item.status for item in normalized}
    expected = [rubric_id for rubric_id in expected_rubric_ids if rubric_id]
    if not expected or any(rubric_id not in by_id for rubric_id in expected):
        return None
    statuses = [by_id[rubric_id] for rubric_id in expected]
    if any(status in {"unknown", "unreadable"} for status in statuses):
        return None
    if all(status == "correct" for status in statuses):
        return "correct"
    if any(status in {"correct", "partial"} for status in statuses):
        return "partial"
    if all(status == "incorrect" for status in statuses):
        return "incorrect"
    return None
