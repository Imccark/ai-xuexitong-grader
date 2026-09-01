from __future__ import annotations

import json

from PIL import Image

from tools.evaluation.core.codex_judge_packets import (
    _normalise_question_id,
    _question_id_matches,
    prepare_packets,
)


def test_codex_packet_question_id_normalization_handles_ocr_dots_and_subparts() -> None:
    assert _normalise_question_id("1.21(1)") == "1.2.1 (1)"
    assert _normalise_question_id("1.2.1（2）") == "1.2.1 (2)"
    assert _question_id_matches("1.21(1)", "1.2.1")
    assert _question_id_matches("(2)", "1.1.1 (2)")
    assert not _question_id_matches("1.2.2", "1.1.1 (2)")


def test_codex_packets_keep_candidate_out_of_blind_context(workspace_tmp_path) -> None:
    root = workspace_tmp_path / "repo"
    student_hash = "a" * 64
    artifact = root / "第一周" / "agent_artifacts" / student_hash
    page_root = artifact / "pages" / "page_1"
    page_root.mkdir(parents=True)
    (page_root / "original.png").write_bytes(b"original")
    (page_root / "enhanced.png").write_bytes(b"enhanced")
    (artifact / "candidate_result.json").write_text(
        json.dumps(
            {
                "assignment_id": "第一周",
                "question_results": {
                    "1.1": {
                        "verdict": "correct",
                        "confidence": 0.91,
                        "transcription": [{"page": 1, "text": "x=1"}],
                        "evidence_refs": [{"page": 1, "bbox": [1, 2, 3, 4]}],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest_dir = root / "evaluation" / "answer_manifests" / "第一周"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps(
            {
                "questions": {
                    "1.1": {
                        "question_type": "calculation",
                        "problem": "solve",
                        "reference_answer": "x=-1",
                        "rubric_items": [],
                        "critical_symbols": ["-"],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = prepare_packets(
        root=root,
        manifest_root=manifest_dir.parent,
        output_root=root / "evaluation" / "codex_judge_packets",
        max_students=1,
        max_questions=1,
    )

    assert report["packet_count"] == 1
    assert report["requires_api_key"] is False
    packet_dir = next((root / "evaluation" / "codex_judge_packets").glob("*/blind_context.json")).parent
    blind = json.loads((packet_dir / "blind_context.json").read_text(encoding="utf-8"))
    candidate = json.loads((packet_dir / "candidate_context.json").read_text(encoding="utf-8"))
    assert "candidate" not in blind
    assert '"verdict"' not in json.dumps(blind, ensure_ascii=False)
    assert candidate["candidate"]["verdict"] == "correct"
    assert blind["image_pages"][0]["variants"]["original"].endswith("original.png")


def test_codex_packets_redact_identity_from_blind_text_and_images(workspace_tmp_path) -> None:
    root = workspace_tmp_path / "repo"
    student_hash = "b" * 64
    artifact = root / "第一周" / "agent_artifacts" / student_hash
    page_root = artifact / "pages" / "page_1"
    page_root.mkdir(parents=True)
    for name in ("original.png", "normalized.png"):
        Image.new("RGB", (200, 200), "white").save(page_root / name)
    (artifact / "candidate_result.json").write_text(
        json.dumps(
            {
                "assignment_id": "第一周",
                "question_results": {
                    "1.1": {
                        "verdict": "correct",
                        "confidence": 0.91,
                        "transcription": [
                            {"page": 1, "bbox": [10, 10, 250, 80], "text": "姓名：张三 学号：12345678901"},
                            {"page": 1, "bbox": [10, 90, 190, 150], "text": "x=1"},
                        ],
                        "evidence_refs": [{"page": 1, "bbox": [10, 90, 190, 150]}],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest_dir = root / "evaluation" / "answer_manifests" / "第一周"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps(
            {"questions": {"1.1": {"question_type": "calculation", "problem": "solve", "reference_answer": "x=1"}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = prepare_packets(
        root=root,
        manifest_root=manifest_dir.parent,
        output_root=root / "evaluation" / "codex_judge_packets",
        max_students=1,
        max_questions=1,
    )
    assert report["contains_student_names"] is False
    packet_dir = next((root / "evaluation" / "codex_judge_packets").glob("*/blind_context.json")).parent
    blind_text = (packet_dir / "blind_context.json").read_text(encoding="utf-8")
    assert "张三" not in blind_text
    assert "12345678901" not in blind_text
    blind = json.loads(blind_text)
    assert "anonymized" in blind["image_pages"][0]["variants"]["original"]


def test_codex_packets_recovers_subquestion_page_from_page_evidence(workspace_tmp_path) -> None:
    root = workspace_tmp_path / "repo"
    student_hash = "c" * 64
    artifact = root / "第一周" / "agent_artifacts" / student_hash
    for page in (1, 4):
        page_root = artifact / "pages" / f"page_{page}"
        page_root.mkdir(parents=True)
        Image.new("RGB", (120, 120), "white").save(page_root / "original.png")
    (artifact / "page_evidence.json").write_text(
        json.dumps({"observations": [{"page": 1, "questions": [{"question_id": "(2)", "bbox": [1, 1, 100, 100]}]}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact / "candidate_result.json").write_text(
        json.dumps(
            {
                "assignment_id": "第一周",
                "question_results": {
                    "1.1.1 (2)": {
                        "verdict": "unreadable",
                        "transcription": [],
                        "evidence_refs": [{"page": 4, "bbox": [1, 1, 100, 100]}],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest_dir = root / "evaluation" / "answer_manifests" / "第一周"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"questions": {"1.1.1 (2)": {"question_type": "calculation", "problem": "solve", "reference_answer": "x=1"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    prepare_packets(
        root=root,
        manifest_root=manifest_dir.parent,
        output_root=root / "evaluation" / "codex_judge_packets",
        max_students=1,
        max_questions=1,
    )
    packet_dir = next((root / "evaluation" / "codex_judge_packets").glob("*/blind_context.json")).parent
    pages = json.loads((packet_dir / "blind_context.json").read_text(encoding="utf-8"))["image_pages"]
    assert [item["page"] for item in pages] == [4, 1]


def test_codex_packets_keep_all_bounded_pages_for_compound_rescue(workspace_tmp_path) -> None:
    root = workspace_tmp_path / "repo"
    student_hash = "d" * 64
    artifact = root / "第一周" / "agent_artifacts" / student_hash
    for page in range(1, 6):
        page_root = artifact / "pages" / f"page_{page}"
        page_root.mkdir(parents=True)
        Image.new("RGB", (120, 120), "white").save(page_root / "original.png")
    (artifact / "candidate_result.json").write_text(
        json.dumps(
            {
                "assignment_id": "第一周",
                "question_results": {
                    "1.2.2": {
                        "verdict": "incorrect",
                        "transcription": [],
                        "evidence_refs": [{"page": 3, "bbox": [1, 1, 100, 100]}],
                        "attempt_history": [
                            {"stage": "question_locator", "outcome": "located_with_full_page_context"}
                        ],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest_dir = root / "evaluation" / "answer_manifests" / "第一周"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps(
            {
                "questions": {
                    "1.2.2": {
                        "problem": r"\textbf{(1)} 判断；\textbf{(2)} 表示 beta",
                        "reference_answer": "answer",
                        "rubric_items": [
                            {"id": "subpart_1", "requirement": "one"},
                            {"id": "subpart_2", "requirement": "two"},
                        ],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    prepare_packets(
        root=root,
        manifest_root=manifest_dir.parent,
        output_root=root / "evaluation" / "codex_judge_packets",
        max_students=1,
        max_questions=1,
    )
    packet_dir = next((root / "evaluation" / "codex_judge_packets").glob("*/blind_context.json")).parent
    pages = json.loads((packet_dir / "blind_context.json").read_text(encoding="utf-8"))["image_pages"]
    page_numbers = [item["page"] for item in pages]
    assert 1 in page_numbers
    assert 3 in page_numbers
    assert len(page_numbers) == 4
