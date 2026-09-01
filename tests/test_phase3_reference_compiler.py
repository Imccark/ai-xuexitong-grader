from __future__ import annotations

from pathlib import Path

from grading_graph.reference_compiler import (
    compile_reference_answer,
    match_observed_questions,
    parse_question_blocks,
)


def test_reference_parser_extracts_problem_sections_and_subquestions() -> None:
    source = r"""
\section{4.2 行列式}
\subsection*{练习 4.2.1}
题目：计算面积。
\subsection*{答案}
答案内容。
\problem{4.2.2}
题目：证明结论。
\begin{solution}
证明过程。
\end{solution}
"""
    blocks = parse_question_blocks(source)
    assert [block["question_id"] for block in blocks] == ["4.2.1", "4.2.2"]
    assert blocks[1]["question_type"] == "proof"
    assert "证明过程" in blocks[1]["reference_answer"]
    assert "题目：计算面积" in blocks[0]["problem"]
    assert "题目：计算面积" not in blocks[0]["reference_answer"]
    assert blocks[0]["aliases"] == ["练习 4.2.1"]


def test_reference_compiler_writes_deterministic_cache_and_invalidates_on_change(workspace_tmp_path: Path) -> None:
    tex_path = workspace_tmp_path / "answer.tex"
    tex_path.write_text("\\subsection*{练习 1.1.1}\n答案 A。\n", encoding="utf-8")
    cache_dir = workspace_tmp_path / "cache"
    first = compile_reference_answer(tex_path, cache_dir=cache_dir, assignment_id="test")
    second = compile_reference_answer(tex_path, cache_dir=cache_dir, assignment_id="test")
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.questions["1.1.1"].character_count > 0
    compiled_question = first.questions["1.1.1"]
    assert compiled_question.question_type == "other"
    assert compiled_question.heading == "练习 1.1.1"
    assert compiled_question.problem
    assert compiled_question.reference_answer
    assert compiled_question.rubric_items
    assert compiled_question.source_range is not None
    assert len(list(cache_dir.glob("*.json"))) == 1

    tex_path.write_text("\\subsection*{练习 1.1.1}\n答案 B。\n", encoding="utf-8")
    changed = compile_reference_answer(tex_path, cache_dir=cache_dir, assignment_id="test")
    assert changed.answer_hash != first.answer_hash
    assert changed.questions["1.1.1"].sha256 != first.questions["1.1.1"].sha256


def test_reference_compiler_parses_all_current_weeks(workspace_tmp_path: Path) -> None:
    root = Path.cwd()
    answer_paths = sorted(path for path in root.glob("第*周/answer.tex") if path.is_file())
    assert len(answer_paths) == 13
    for path in answer_paths:
        manifest = compile_reference_answer(
            path,
            cache_dir=workspace_tmp_path / path.parent.name,
            assignment_id=path.parent.name,
        )
        assert manifest.reference_status == "ready"
        assert manifest.questions
        assert all(slice_ref.sha256 and slice_ref.character_count >= 0 for slice_ref in manifest.questions.values())
        source_characters = len(path.read_text(encoding="utf-8"))
        average_slice_ratio = (
            sum(slice_ref.character_count for slice_ref in manifest.questions.values())
            / len(manifest.questions)
            / source_characters
        )
        assert average_slice_ratio < 0.2, (path, average_slice_ratio)


def test_mismatched_question_ids_are_blocked() -> None:
    manifest_questions = {"4.2.1", "4.2.2"}
    result = match_observed_questions(["4.2.1", "9.9.9"], manifest_questions)
    assert result["matched"] == ["4.2.1"]
    assert result["unmatched"] == ["9.9.9"]
    assert result["status"] == "reference_mismatch"


def test_manifest_aliases_match_to_canonical_question_id() -> None:
    result = match_observed_questions(
        ["旧版 4.2.1 第一问"],
        {
            "4.2.1 (1)": {
                "aliases": ["旧版 4.2.1 第一问"],
            }
        },
    )
    assert result == {
        "status": "ready",
        "matched": ["4.2.1 (1)"],
        "unmatched": [],
    }


def test_compiled_manifest_artifacts_cover_all_weeks_without_claiming_teacher_confirmation() -> None:
    import json

    summary = json.loads((Path.cwd() / "evaluation" / "answer_manifests" / "summary.json").read_text(encoding="utf-8"))
    assert summary["week_count"] == 13
    assert summary["ready_count"] == 13
    assert summary["compiler_version"] == "reference-compiler-v4"
    assert summary["max_average_slice_ratio"] < 0.2
    assert summary["teacher_confirmation"] == "pending"
