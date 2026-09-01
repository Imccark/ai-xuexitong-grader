from __future__ import annotations

from pathlib import Path

import pytest

from app.grading_graph.nodes.page_router import build_question_jobs
from app.grading_graph.nodes.page_observer import PageObserver
from app.grading_graph.nodes.transcriber import LiteralTranscriber, TranscriptionProviderError
from app.grading_graph.pipeline import _expand_ambiguous_shared_routes, _question_for_span
from app.grading_graph.schemas import AnswerManifest, AnswerSliceRef, FileRef, PageArtifact, TranscriptionSpan


def _manifest() -> AnswerManifest:
    return AnswerManifest(
        assignment_id="第一周",
        answer_hash="a" * 64,
        compiler_version="test",
        questions={
            "1.1.1": AnswerSliceRef(
                question_id="1.1.1",
                artifact_ref="slices/one.tex",
                sha256="b" * 64,
                character_count=12,
            )
        },
    )


def test_router_isolates_question_jobs_and_marks_low_confidence_risk() -> None:
    result = build_question_jobs(
        [
            {
                "page": 1,
                "page_type": "assignment",
                "questions": [
                    {"question_id": "1.1.1", "bbox": [10, 20, 200, 300], "confidence": 0.95},
                    {"question_id": "1.1.1", "bbox": [20, 310, 200, 500], "confidence": 0.55},
                ],
            }
        ],
        _manifest(),
        confidence_threshold=0.8,
    )
    assert list(result["question_jobs"]) == ["1.1.1"]
    assert result["question_jobs"]["1.1.1"].pages == [1]
    assert len(result["question_jobs"]["1.1.1"].roi_refs) == 2
    assert result["question_jobs"]["1.1.1"].route == "risk"
    assert result["status"] == "ready"


def test_router_blocks_unmatched_question_without_answer_slice() -> None:
    result = build_question_jobs(
        [{"page": 1, "page_type": "assignment", "questions": [{"question_id": "9.9.9", "bbox": [0, 0, 10, 10], "confidence": 0.99}]}],
        _manifest(),
    )
    assert result["status"] == "reference_mismatch"
    assert result["question_jobs"]["9.9.9"].route == "mismatch"
    assert result["question_jobs"]["9.9.9"].answer_slice is None


def test_router_maps_reviewed_manifest_alias_to_canonical_question() -> None:
    manifest = AnswerManifest(
        assignment_id="test",
        answer_hash="a" * 64,
        compiler_version="test",
        questions={
            "1.1.1": AnswerSliceRef(
                question_id="1.1.1",
                artifact_ref="reference_slices/a.tex",
                sha256="b" * 64,
                character_count=3,
                aliases=["旧版1.1.1第一问"],
                question_type="calculation",
            )
        },
    )
    result = build_question_jobs(
        [{
            "page": 1,
            "page_type": "assignment",
            "questions": [{"question_id": "旧版1.1.1第一问", "bbox": [0, 0, 10, 10], "confidence": 0.95}],
        }],
        manifest,
    )
    assert result["status"] == "ready"
    assert list(result["question_jobs"]) == ["1.1.1"]
    assert result["question_jobs"]["1.1.1"].question_type == "calculation"


def test_router_resolves_ocr_missing_decimal_segment() -> None:
    manifest = AnswerManifest(
        assignment_id="test",
        answer_hash="a" * 64,
        compiler_version="test",
        questions={
            "1.1.5": AnswerSliceRef(
                question_id="1.1.5",
                artifact_ref="reference_slices/a.tex",
                sha256="b" * 64,
                character_count=3,
            )
        },
    )
    result = build_question_jobs(
        [{"page": 1, "page_type": "assignment", "questions": [{"question_id": "1.15", "bbox": [0, 0, 10, 10], "confidence": 0.95}]}],
        manifest,
    )
    assert list(result["question_jobs"]) == ["1.1.5"]


def test_router_collapses_manifest_combined_subquestion() -> None:
    manifest = AnswerManifest(
        assignment_id="test",
        answer_hash="a" * 64,
        compiler_version="test",
        questions={
            "1.2.1": AnswerSliceRef(
                question_id="1.2.1",
                artifact_ref="reference_slices/a.tex",
                sha256="b" * 64,
                character_count=3,
            )
        },
    )
    result = build_question_jobs(
        [{"page": 1, "page_type": "assignment", "questions": [{"question_id": "1.2.1 (1)", "bbox": [0, 0, 10, 10], "confidence": 0.95}]}],
        manifest,
    )
    assert list(result["question_jobs"]) == ["1.2.1"]
    assert result["status"] == "ready"


def test_shared_unsuffixed_roi_is_expanded_for_answer_blind_locator() -> None:
    manifest = AnswerManifest(
        assignment_id="test",
        answer_hash="a" * 64,
        compiler_version="test",
        questions={
            suffix: AnswerSliceRef(
                question_id=suffix,
                artifact_ref=f"reference_slices/{suffix[-2]}.tex",
                sha256=("b" if suffix.endswith("(1)") else "c") * 64,
                character_count=3,
            )
            for suffix in ("1.1.1 (1)", "1.1.1 (2)")
        },
    )
    routed = build_question_jobs(
        [{"page": 4, "page_type": "assignment", "questions": [{"question_id": "1.1.1", "bbox": [0, 0, 10, 10], "confidence": 0.5}]}],
        manifest,
    )
    jobs = routed["question_jobs"]
    mapping = {(4, 1): ["1.1.1 (1)", "1.1.1 (2)"]}
    pages = [
        PageArtifact(
            page=page,
            original=FileRef(path=f"page_{page}.png", sha256="d" * 64),
            normalized=FileRef(path=f"page_{page}.png", sha256="d" * 64),
            quality={"width": 1000, "height": 1400},
        )
        for page in range(1, 6)
    ]
    events = _expand_ambiguous_shared_routes(jobs, mapping, pages)
    assert len(events) == 2
    for job in jobs.values():
        assert job.route == "risk"
        assert any(ref.span_id == "ambiguous-p4-q1" for ref in job.roi_refs)
        assert {ref.page for ref in job.roi_refs} == {1, 2, 3, 4}


def test_span_assignment_falls_back_to_maximum_roi_overlap() -> None:
    span = TranscriptionSpan(span_id="s", page=1, bbox=(0, 0, 100, 100), text="x", confidence=0.9, readability="clear")
    observations = [
        {"question_id": "left", "bbox": [0, 0, 40, 100]},
        {"question_id": "right", "bbox": [60, 0, 100, 100]},
    ]
    assert _question_for_span(span, observations) == "left"


class RecordingProvider:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.prompts: list[str] = []
        self.image_refs: list[str | None] = []

    def complete_json(self, prompt: str, schema: dict, image_ref: str | None = None) -> dict:
        self.prompts.append(prompt)
        self.image_refs.append(image_ref)
        return self.response


def test_literal_transcriber_does_not_receive_answer_and_keeps_unknown_symbols() -> None:
    provider = RecordingProvider(
        {
            "spans": [
                {
                    "span_id": "p1-r1-l1",
                    "page": 1,
                    "bbox": [10, 20, 100, 80],
                    "text": "x_1 = unknown",
                    "symbol_candidates": [{"symbol": "minus", "confidence": 0.41}, {"symbol": "blank", "confidence": 0.59}],
                    "readability": "uncertain",
                    "confidence": 0.41,
                }
            ]
        }
    )
    transcriber = LiteralTranscriber(provider)
    spans = transcriber.transcribe(
        Path("page_1.png"),
        page=1,
        roi_refs=[{"span_id": "p1-r1", "bbox": [0, 0, 120, 120]}],
    )
    assert spans[0].readability == "uncertain"
    assert spans[0].symbol_candidates[0].symbol == "minus"
    assert "reference_answer" not in provider.prompts[0]
    assert "答案" not in provider.prompts[0]
    assert "JSON" in provider.prompts[0]
    assert provider.image_refs == ["page_1.png"]


def test_literal_transcriber_rejects_high_confidence_missing_bbox() -> None:
    provider = RecordingProvider(
        {"spans": [{"span_id": "bad", "page": 1, "text": "x", "readability": "clear", "confidence": 0.99}]}
    )
    with pytest.raises(TranscriptionProviderError, match="bbox"):
        LiteralTranscriber(provider).transcribe(Path("page_1.png"), page=1, roi_refs=[])


def test_literal_transcriber_normalizes_compact_qwen_span() -> None:
    provider = RecordingProvider(
        {
            "spans": [
                {
                    "page": 99,
                    "bbox": [0, 0, 10, 10],
                    "text": "-1",
                    "symbol_candidates": {"-1": ["-1"]},
                    "confidence": 0.9,
                }
            ]
        }
    )
    span = LiteralTranscriber(provider).transcribe(Path("page_1.png"), page=1, roi_refs=[])[0]
    assert span.page == 1
    assert span.span_id == "p1-span-1"
    assert span.readability == "clear"
    assert span.symbol_candidates[0].symbol == "minus"


def test_literal_transcriber_accepts_qwen_symbols_alias() -> None:
    provider = RecordingProvider(
        {
            "spans": [
                {
                    "page": 1,
                    "bbox": [0, 0, 10, 10],
                    "text": "x = -1",
                    "symbols": [{"type": "matrix", "content": "-1"}],
                    "confidence": 0.8,
                }
            ]
        }
    )
    span = LiteralTranscriber(provider).transcribe(Path("page_1.png"), page=1, roi_refs=[])[0]
    assert span.text == "x = -1"
    assert span.symbol_candidates == []


def test_page_observer_validates_question_regions_without_answer_context() -> None:
    class ObserverProvider:
        def complete_json(self, prompt, schema, image_ref=None):
            assert "标准答案" in prompt
            assert image_ref == "page_1.png"
            return {
                "page_type": "assignment",
                "questions": [{"question_id": "1.1.1", "bbox": [1, 2, 30, 40], "confidence": 0.91}],
            }

    observation = PageObserver(ObserverProvider()).observe("page_1.png", page=1)
    assert observation["questions"][0]["question_id"] == "1.1.1"
    assert observation["questions"][0]["bbox"] == [1, 2, 30, 40]
    assert observation["questions"][0]["artifact_ref"].endswith("page_1.png")


def test_page_observer_normalizes_qwen_legacy_homework_shape() -> None:
    class ObserverProvider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {
                "page_type": "homework",
                "problems": [{"id": "1.1", "bbox": [0, 0, 10, 10], "confidence": 0.95}],
            }

    result = PageObserver(ObserverProvider()).observe("page.png", page=1)
    assert result["page_type"] == "assignment"
    assert result["questions"][0]["question_id"] == "1.1"


def test_page_observer_accepts_space_separated_qwen_bbox() -> None:
    class ObserverProvider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {"page_type": "assignment", "questions": [{"question_id": "1.1.4", "bbox": "30 40 90 120", "confidence": 0.9}]}

    result = PageObserver(ObserverProvider()).observe("page.png", page=1)
    assert result["questions"][0]["bbox"] == [30, 40, 90, 120]


def test_page_observer_accepts_qwen_question_regions_alias() -> None:
    class ObserverProvider:
        def complete_json(self, prompt, schema, image_ref=None):
            return {"page_type": "handwritten_homework", "question_regions": [{"question_number": "1.1.1(1)", "bbox": [1, 2, 30, 40], "confidence": 0.91}]}

    result = PageObserver(ObserverProvider()).observe("page.png", page=1)
    assert result["page_type"] == "assignment"
    assert result["questions"][0]["question_id"] == "1.1.1(1)"
