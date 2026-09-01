from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from app.grading_graph.adapters.batch import run_student_candidate_from_images
from app.grading_graph.budget import BudgetLedger
from app.grading_graph.checkpoint import open_sqlite_checkpointer
from app.grading_graph.graph import build_image_grading_graph
from app.grading_graph.nodes.image_quality import RECTIFICATION_VERSION
from app.grading_graph.pipeline import _reassign_cross_page_continuations
from app.grading_graph.schemas import AnswerManifest, AnswerSliceRef, Budget, EvidenceRef, QuestionJob
from app.grading_graph.store import atomic_write_json


class FullPipelineProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.image_refs: list[str | None] = []

    def complete_json(self, prompt: str, schema: dict, image_ref: str | None = None) -> dict:
        self.calls.append(prompt)
        self.image_refs.append(image_ref)
        if "页面和题号观察器" in prompt:
            return {
                "page_type": "assignment",
                "questions": [{"question_id": "1.1.1", "bbox": [0, 0, 100, 100], "confidence": 0.95}],
            }
        if "忠实转写器" in prompt:
            return {
                "spans": [
                    {
                        "span_id": "p1-q1-line1",
                        "page": 1,
                        "bbox": [10, 10, 90, 90],
                        "text": "x = 1",
                        "symbol_candidates": [],
                        "readability": "clear",
                        "confidence": 0.95,
                    }
                ]
            }
        return {
            "verdict": "correct",
            "confidence": 0.95,
            "needs_verification": False,
            "risk_level": "low",
            "evidence_refs": [],
            "rubric_decisions": [],
        }


def _write_png(path: Path) -> None:
    image = Image.new("RGB", (100, 100), "white")
    pixels = image.load()
    for x in range(10, 90):
        for y in range(10, 90):
            pixels[x, y] = (0, 0, 0)
    image.save(path, format="PNG")


def test_full_candidate_pipeline_prepares_pages_and_preserves_formal_result(workspace_tmp_path) -> None:
    processed_student_dir = workspace_tmp_path / "processed_images" / "student-1"
    processed_student_dir.mkdir(parents=True)
    _write_png(processed_student_dir / "page_1.png")

    manifest_dir = workspace_tmp_path / "manifest"
    slice_dir = manifest_dir / "reference_slices"
    slice_dir.mkdir(parents=True)
    (slice_dir / "answer.tex").write_text("x=1", encoding="utf-8")
    manifest = AnswerManifest(
        assignment_id="第一周",
        answer_hash="a" * 64,
        compiler_version="test",
        questions={
            "1.1.1": AnswerSliceRef(
                question_id="1.1.1",
                artifact_ref="reference_slices/answer.tex",
                sha256="b" * 64,
                character_count=3,
            )
        },
    )
    manifest_path = manifest_dir / "manifest.json"
    atomic_write_json(manifest_path, manifest.model_dump(mode="json"))

    formal_dir = workspace_tmp_path / "第一周" / "results"
    formal_dir.mkdir(parents=True)
    formal_path = formal_dir / "student-1.txt"
    formal_path.write_text("legacy formal result", encoding="utf-8")
    before = formal_path.read_bytes()
    provider = FullPipelineProvider()
    candidate = run_student_candidate_from_images(
        provider=provider,
        processed_student_dir=processed_student_dir,
        answer_manifest_path=manifest_path,
        artifact_root=workspace_tmp_path / "第一周",
        assignment_id="第一周",
        student_id="student-1",
        run_id="pipeline-run-1",
        budget=Budget(max_calls=5, max_input_tokens=20000, max_output_tokens=2000),
        checkpoint_path=workspace_tmp_path / "第一周" / "checkpoints.sqlite",
        cache_dir=workspace_tmp_path / "cache",
    )

    assert candidate.overall.value == "all_correct"
    assert candidate.question_results["1.1.1"].transcription[0].text == "x = 1"
    assert candidate.budget_usage["calls"] == 3
    assert formal_path.read_bytes() == before
    assert (workspace_tmp_path / "第一周" / "agent_artifacts").is_dir()
    artifact_root = workspace_tmp_path / "第一周" / "agent_artifacts"
    student_artifact = next(path for path in artifact_root.iterdir() if path.is_dir())
    evidence = json.loads((student_artifact / "input_manifest.json").read_text(encoding="utf-8"))
    assert evidence["preprocess_version"] == RECTIFICATION_VERSION
    assert evidence["pages"][0]["rectified"]["path"] == str(
        (student_artifact / "pages" / "page_1" / "rectified.png").resolve()
    )
    assert evidence["question_jobs"]["1.1.1"]["roi_refs"][0]["artifact_ref"] == str(
        (student_artifact / "pages" / "page_1" / "normalized.png").resolve()
    )
    assert str((student_artifact / "pages" / "page_1" / "normalized.png").resolve()) in provider.image_refs
    assert any("x=1" in prompt for prompt in provider.calls)


def test_image_preparation_is_checkpointed_before_grading_and_resumes_without_repeating_calls(workspace_tmp_path) -> None:
    processed_student_dir = workspace_tmp_path / "processed_images" / "student-resume"
    processed_student_dir.mkdir(parents=True)
    _write_png(processed_student_dir / "page_1.png")
    manifest_dir = workspace_tmp_path / "manifest-resume"
    slice_dir = manifest_dir / "reference_slices"
    slice_dir.mkdir(parents=True)
    (slice_dir / "answer.tex").write_text("x=1", encoding="utf-8")
    manifest_path = manifest_dir / "manifest.json"
    atomic_write_json(
        manifest_path,
        AnswerManifest(
            assignment_id="第一周",
            answer_hash="a" * 64,
            compiler_version="test",
            questions={
                "1.1.1": AnswerSliceRef(
                    question_id="1.1.1",
                    artifact_ref="reference_slices/answer.tex",
                    sha256="b" * 64,
                    character_count=3,
                )
            },
        ).model_dump(mode="json"),
    )
    budget = Budget(max_calls=5, max_input_tokens=20000, max_output_tokens=2000)
    launch = {
        "schema_version": "1.0",
        "graph_version": "test",
        "run_id": "image-parent-resume",
        "assignment_id": "第一周",
        "student_id": "student-resume",
        "budget": budget.model_dump(mode="json"),
        "processed_student_dir": str(processed_student_dir),
        "answer_manifest_path": str(manifest_path),
        "artifact_root": str(workspace_tmp_path / "第一周"),
        "local_layout_config": {"enabled": False},
    }
    provider = FullPipelineProvider()
    checkpoint_path = workspace_tmp_path / "image-parent.sqlite"
    config = {"configurable": {"thread_id": "image-parent-resume"}}

    with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
        first_ledger = BudgetLedger(budget.model_dump(mode="json"))
        app = build_image_grading_graph(provider, checkpointer=checkpointer, budget_ledger=first_ledger)
        stream = app.stream(launch, config=config)
        first_event = next(stream)
        assert "prepare_image_input" in first_event
        stream.close()
    preparation_call_count = len(provider.calls)
    assert preparation_call_count == 2

    with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
        resumed_ledger = BudgetLedger(budget.model_dump(mode="json"))
        app = build_image_grading_graph(provider, checkpointer=checkpointer, budget_ledger=resumed_ledger)
        output = app.invoke(None, config=config)

    assert output["candidate"]["overall"] == "all_correct"
    assert len(provider.calls) == preparation_call_count + 1
    assert output["candidate"]["budget_usage"]["calls"] == 3


def test_page_observer_can_correct_residual_orientation_once(workspace_tmp_path, monkeypatch) -> None:
    from app.grading_graph.nodes import image_quality

    processed = workspace_tmp_path / "processed_images" / "student-rotation"
    processed.mkdir(parents=True)
    image = Image.new("RGB", (120, 80), "white")
    for x in range(10, 110):
        for y in range(20, 60):
            image.putpixel((x, y), (0, 0, 0))
    image.save(processed / "page_1.png")
    manifest_dir = workspace_tmp_path / "manifest-rotation"
    (manifest_dir / "reference_slices").mkdir(parents=True)
    (manifest_dir / "reference_slices" / "answer.tex").write_text("x=1", encoding="utf-8")
    manifest = AnswerManifest(
        assignment_id="第一周",
        answer_hash="a" * 64,
        compiler_version="test",
        questions={
            "1.1.1": AnswerSliceRef(
                question_id="1.1.1",
                artifact_ref="reference_slices/answer.tex",
                sha256="b" * 64,
                character_count=3,
            )
        },
    )
    manifest_path = manifest_dir / "manifest.json"
    atomic_write_json(manifest_path, manifest.model_dump(mode="json"))
    monkeypatch.setattr(
        image_quality,
        "classify_document_orientation",
        lambda _image: {
            "available": True,
            "applied": False,
            "reason": "low_orientation_confidence",
            "rotation_degrees_clockwise": 0,
            "predicted_orientation_degrees_clockwise": 90,
            "confidence": 0.6,
            "margin": 0.1,
            "orientation_scores": {},
            "model": "fake",
            "model_sha256": "fake",
        },
    )

    class ResidualRotationProvider(FullPipelineProvider):
        observations = 0

        def complete_json(self, prompt: str, schema: dict, image_ref: str | None = None) -> dict:
            if "页面和题号观察器" in prompt:
                self.observations += 1
                payload = super().complete_json(prompt, schema, image_ref=image_ref)
                payload["rotation_degrees_clockwise"] = 90 if self.observations == 1 else 0
                payload["orientation_confidence"] = 0.98
                return payload
            return super().complete_json(prompt, schema, image_ref=image_ref)

    provider = ResidualRotationProvider()
    candidate = run_student_candidate_from_images(
        provider=provider,
        processed_student_dir=processed,
        answer_manifest_path=manifest_path,
        artifact_root=workspace_tmp_path / "第一周",
        assignment_id="第一周",
        student_id="student-rotation",
        run_id="orientation-fallback",
        budget=Budget(max_calls=6, max_input_tokens=20000, max_output_tokens=2000),
    )
    assert provider.observations == 2
    artifact_dir = next((workspace_tmp_path / "第一周" / "agent_artifacts").iterdir())
    input_manifest = json.loads((artifact_dir / "input_manifest.json").read_text(encoding="utf-8"))
    assert input_manifest["pages"][0]["quality"]["geometry"]["orientation"]["reason"] == "multimodal_page_observer_fallback"
    assert input_manifest["pages"][0]["quality"]["rectified_quality"]["width"] == 80
    assert candidate.errors == []


def test_pipeline_rescues_from_image_when_transcription_fails(workspace_tmp_path) -> None:
    processed_student_dir = workspace_tmp_path / "processed_images" / "student-1"
    processed_student_dir.mkdir(parents=True)
    _write_png(processed_student_dir / "page_1.png")
    manifest_dir = workspace_tmp_path / "manifest" / "reference_slices"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "answer.tex").write_text("x=1", encoding="utf-8")
    manifest = AnswerManifest(
        assignment_id="第一周",
        answer_hash="a" * 64,
        compiler_version="test",
        questions={"1.1.1": AnswerSliceRef(question_id="1.1.1", artifact_ref="reference_slices/answer.tex", sha256="b" * 64, character_count=3)},
    )
    manifest_path = manifest_dir.parent / "manifest.json"
    atomic_write_json(manifest_path, manifest.model_dump(mode="json"))

    class FailingTranscriberProvider(FullPipelineProvider):
        def complete_json(self, prompt, schema, image_ref=None):
            if "忠实转写器" in prompt:
                raise TimeoutError("synthetic transcription failure")
            return super().complete_json(prompt, schema, image_ref=image_ref)

    provider = FailingTranscriberProvider()
    candidate = run_student_candidate_from_images(
        provider=provider,
        processed_student_dir=processed_student_dir,
        answer_manifest_path=manifest_path,
        artifact_root=workspace_tmp_path / "第一周",
        assignment_id="第一周",
        student_id="student-1",
        run_id="pipeline-failure-run",
        budget=Budget(max_calls=6, max_input_tokens=20000, max_output_tokens=2000),
    )

    assert candidate.overall.value == "all_correct"
    result = candidate.question_results["1.1.1"]
    assert result.evidence_status == "image_only"
    assert result.resolution_status == "rescued"
    assert any("逐题批改器" in prompt for prompt in provider.calls)


def test_pipeline_rescues_router_miss_with_bounded_full_page_images(workspace_tmp_path) -> None:
    processed_student_dir = workspace_tmp_path / "processed_images" / "student-1"
    processed_student_dir.mkdir(parents=True)
    _write_png(processed_student_dir / "page_1.png")
    manifest_dir = workspace_tmp_path / "manifest" / "reference_slices"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "answer.tex").write_text("x=1", encoding="utf-8")
    manifest = AnswerManifest(
        assignment_id="第一周",
        answer_hash="a" * 64,
        compiler_version="test",
        questions={
            "1.1.1": AnswerSliceRef(
                question_id="1.1.1",
                artifact_ref="reference_slices/answer.tex",
                sha256="b" * 64,
                character_count=3,
            )
        },
    )
    manifest_path = manifest_dir.parent / "manifest.json"
    atomic_write_json(manifest_path, manifest.model_dump(mode="json"))

    class MissingRouteProvider(FullPipelineProvider):
        def complete_json(self, prompt, schema, image_ref=None):
            if "页面和题号观察器" in prompt:
                self.calls.append(prompt)
                self.image_refs.append(image_ref)
                return {"page_type": "assignment", "questions": []}
            return super().complete_json(prompt, schema, image_ref=image_ref)

    provider = MissingRouteProvider()
    candidate = run_student_candidate_from_images(
        provider=provider,
        processed_student_dir=processed_student_dir,
        answer_manifest_path=manifest_path,
        artifact_root=workspace_tmp_path / "第一周",
        assignment_id="第一周",
        student_id="student-1",
        run_id="router-miss-rescue-run",
        budget=Budget(max_calls=4, max_input_tokens=20000, max_output_tokens=2000),
    )

    result = candidate.question_results["1.1.1"]
    assert result.verdict.value == "correct"
    assert result.evidence_status == "image_only"
    assert result.resolution_status == "rescued"
    assert any("逐题批改器" in prompt for prompt in provider.calls)
    assert any(isinstance(ref, str) and ref.endswith("normalized.png") for ref in provider.image_refs)


def test_pipeline_treats_observer_failure_as_recoverable_full_page_warning(workspace_tmp_path) -> None:
    processed_student_dir = workspace_tmp_path / "processed_images" / "student-1"
    processed_student_dir.mkdir(parents=True)
    _write_png(processed_student_dir / "page_1.png")
    manifest_dir = workspace_tmp_path / "manifest" / "reference_slices"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "answer.tex").write_text("x=1", encoding="utf-8")
    manifest = AnswerManifest(
        assignment_id="第一周",
        answer_hash="a" * 64,
        compiler_version="test",
        questions={
            "1.1.1": AnswerSliceRef(
                question_id="1.1.1",
                artifact_ref="reference_slices/answer.tex",
                sha256="b" * 64,
                character_count=3,
            )
        },
    )
    manifest_path = manifest_dir.parent / "manifest.json"
    atomic_write_json(manifest_path, manifest.model_dump(mode="json"))

    class FailingObserverProvider(FullPipelineProvider):
        def complete_json(self, prompt, schema, image_ref=None):
            if "页面和题号观察器" in prompt:
                raise TimeoutError("synthetic observer failure")
            return super().complete_json(prompt, schema, image_ref=image_ref)

    candidate = run_student_candidate_from_images(
        provider=FailingObserverProvider(),
        processed_student_dir=processed_student_dir,
        answer_manifest_path=manifest_path,
        artifact_root=workspace_tmp_path / "第一周",
        assignment_id="第一周",
        student_id="student-1",
        run_id="observer-full-page-rescue",
        budget=Budget(max_calls=4, max_input_tokens=20000, max_output_tokens=2000),
    )

    assert candidate.question_results["1.1.1"].verdict.value == "correct"
    assert candidate.errors == []
    artifact_dir = next((workspace_tmp_path / "第一周" / "agent_artifacts").iterdir())
    input_manifest = json.loads((artifact_dir / "input_manifest.json").read_text(encoding="utf-8"))
    assert input_manifest["warnings"] == [
        {
            "stage": "page_observer_full_page_rescue",
            "page": 1,
            "error_type": "TimeoutError",
        }
    ]


def test_page_top_continuation_is_moved_to_previous_bottom_question(workspace_tmp_path) -> None:
    page1 = workspace_tmp_path / "page1.png"
    page2 = workspace_tmp_path / "page2.png"
    _write_png(page1)
    _write_png(page2)
    jobs = {
        "1.1.4": QuestionJob(
            question_id="1.1.4",
            pages=[1],
            roi_refs=[EvidenceRef(span_id="p1-q2", page=1, bbox=(0, 480, 1000, 1000), artifact_ref=str(page1))],
        ),
        "1.1.5": QuestionJob(
            question_id="1.1.5",
            pages=[2],
            roi_refs=[EvidenceRef(span_id="p2-q1", page=2, bbox=(0, 100, 1000, 900), artifact_ref=str(page2))],
        ),
    }
    transcriptions = {
        "1.1.4": [],
        "1.1.5": [
            {
                "span_id": "p2-span-1",
                "page": 2,
                "bbox": [10, 20, 900, 80],
                "text": "k=1 时无穷多解 x1=x3+1",
                "readability": "clear",
                "confidence": 0.95,
                "symbol_candidates": [],
            },
            {
                "span_id": "p2-span-2",
                "page": 2,
                "bbox": [10, 110, 900, 300],
                "text": "1.15 A'=...",
                "readability": "clear",
                "confidence": 0.95,
                "symbol_candidates": [],
            },
        ],
    }

    events = _reassign_cross_page_continuations(
        jobs,
        transcriptions,
        {1: str(page1), 2: str(page2)},
    )

    assert [item["span_id"] for item in transcriptions["1.1.4"]] == ["p2-span-1"]
    assert [item["span_id"] for item in transcriptions["1.1.5"]] == ["p2-span-2"]
    assert jobs["1.1.4"].pages == [1, 2]
    assert jobs["1.1.4"].roi_refs[-1].page == 2
    assert events[0]["stage"] == "cross_page_continuation"
