from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.grading_graph.nodes.local_layout import (
    LocalLayoutObserver,
    LocalLayoutSettings,
    normalize_question_label,
    question_label_candidates,
    resolve_question_label,
)
from app.grading_graph.pipeline import build_student_graph_input
from app.grading_graph.schemas import AnswerManifest, AnswerSliceRef, Budget
from app.grading_graph.store import atomic_write_json


class FakeLayoutBackend:
    def __init__(self, boxes: list[dict]) -> None:
        self.boxes = boxes
        self.calls = 0

    def predict(self, image_path: Path | str):
        self.calls += 1
        return [{"res": {"boxes": self.boxes}}]


class FakeQuestionLabelReader:
    def __init__(self, anchors: list[dict]) -> None:
        self.anchors = anchors
        self.page_calls = 0

    def read(self, image_path: Path | str, bbox: tuple[int, int, int, int]):
        return []

    def read_page(self, image_path: Path | str):
        self.page_calls += 1
        return list(self.anchors)


class PreparationProvider:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete_json(self, prompt: str, schema: dict, image_ref=None) -> dict:
        self.prompts.append(prompt)
        if "页面和题号观察器" in prompt:
            return {
                "page_type": "assignment",
                "questions": [
                    {"question_id": "1.1.1", "bbox": [5, 5, 115, 115], "confidence": 0.96}
                ],
            }
        if "忠实转写器" in prompt:
            return {
                "spans": [
                    {
                        "span_id": "line-1",
                        "page": 1,
                        "bbox": [10, 10, 100, 100],
                        "text": "x=1",
                        "symbol_candidates": [],
                        "readability": "clear",
                        "confidence": 0.95,
                    }
                ]
            }
        raise AssertionError("unexpected provider call")


def _manifest(tmp_path: Path) -> tuple[AnswerManifest, Path]:
    manifest_dir = tmp_path / "manifest"
    slices = manifest_dir / "reference_slices"
    slices.mkdir(parents=True)
    answer = slices / "answer.tex"
    answer.write_text("x=1", encoding="utf-8")
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
                aliases=["第 1.1.1 题", "1．1．1"],
            )
        },
    )
    path = manifest_dir / "manifest.json"
    atomic_write_json(path, manifest.model_dump(mode="json"))
    return manifest, path


def _multi_manifest() -> AnswerManifest:
    questions = {}
    for question_id in ("1.1.1", "1.1.2"):
        questions[question_id] = AnswerSliceRef(
            question_id=question_id,
            artifact_ref=f"reference_slices/{question_id}.tex",
            sha256="b" * 64,
            character_count=3,
            aliases=[f"第 {question_id} 题"],
        )
    return AnswerManifest(
        assignment_id="第一周",
        answer_hash="a" * 64,
        compiler_version="test",
        questions=questions,
    )


def _page(path: Path) -> None:
    image = Image.new("RGB", (120, 120), "white")
    for x in range(10, 110):
        for y in range(10, 110):
            image.putpixel((x, y), (0, 0, 0))
    image.save(path)


def _settings(tmp_path: Path) -> LocalLayoutSettings:
    return LocalLayoutSettings.from_mapping(
        {
            "enabled": True,
            "model_name": "PP-DocLayoutV3-test",
            "model_dir": str(tmp_path / "unused-model"),
            "engine": "onnxruntime",
            "allow_model_download": False,
            "min_region_confidence": 0.8,
            "min_question_label_confidence": 0.85,
            "question_id_ocr_enabled": False,
        }
    )


def test_question_label_normalization_and_alias_resolution(workspace_tmp_path) -> None:
    manifest, _ = _manifest(workspace_tmp_path)
    assert normalize_question_label(" 第 1．1．1 题 ") == "1.1.1"
    assert "1.1.1(2)" in question_label_candidates("第1.1.1（2）题")
    assert resolve_question_label("第 1．1．1 题", manifest) == "1.1.1"
    assert resolve_question_label("9.9.9", manifest) is None


def test_question_label_repairs_a_unique_missing_separator() -> None:
    manifest = _multi_manifest()
    assert resolve_question_label("11.2", manifest) == "1.1.2"


def test_default_model_regions_merge_by_page_question_anchors(workspace_tmp_path) -> None:
    image_path = workspace_tmp_path / "page.png"
    Image.new("RGB", (200, 400), "white").save(image_path)
    backend = FakeLayoutBackend(
        [
            {"label": "text", "score": 0.72, "coordinate": [10, 20, 180, 80]},
            {"label": "inline_formula", "score": 0.66, "coordinate": [20, 85, 170, 150]},
            {"label": "paragraph_title", "score": 0.91, "coordinate": [10, 210, 80, 230]},
            {"label": "text", "score": 0.74, "coordinate": [10, 235, 180, 360]},
            {"label": "footer", "score": 0.99, "coordinate": [0, 380, 200, 399]},
        ]
    )
    reader = FakeQuestionLabelReader(
        [
            {"text": "1.1.1", "confidence": 0.98, "bbox": [5, 15, 60, 35]},
            {"text": "11.2", "confidence": 0.93, "bbox": [5, 205, 60, 225]},
        ]
    )
    settings = LocalLayoutSettings.from_mapping(
        {
            "enabled": True,
            "model_name": "PP-DocLayoutV3",
            "engine": "onnxruntime",
            "min_region_confidence": 0.48,
            "min_question_label_confidence": 0.65,
            "min_region_area_ratio": 0.001,
            "question_id_ocr_enabled": True,
            "merge_default_regions": True,
        }
    )
    result = LocalLayoutObserver(
        settings,
        _multi_manifest(),
        backend=backend,
        label_reader=reader,
    ).observe(image_path, page=1)
    assert result["accepted"] is True
    assert result["audit"]["mode"] == "default_model_rule_merge"
    assert [item["question_id"] for item in result["observation"]["questions"]] == [
        "1.1.1",
        "1.1.2",
    ]
    assert [item["bbox"] for item in result["observation"]["questions"]] == [
        [3, 5, 187, 200],
        [3, 200, 187, 370],
    ]
    assert reader.page_calls == 1


def test_default_model_merge_rejects_unresolved_content_before_first_anchor(
    workspace_tmp_path,
) -> None:
    image_path = workspace_tmp_path / "page.png"
    Image.new("RGB", (200, 400), "white").save(image_path)
    backend = FakeLayoutBackend(
        [
            {"label": "text", "score": 0.72, "coordinate": [10, 20, 180, 90]},
            {"label": "text", "score": 0.75, "coordinate": [10, 220, 180, 360]},
        ]
    )
    reader = FakeQuestionLabelReader(
        [{"text": "1.1.2", "confidence": 0.98, "bbox": [5, 205, 60, 225]}]
    )
    settings = LocalLayoutSettings.from_mapping(
        {
            "enabled": True,
            "engine": "onnxruntime",
            "min_region_confidence": 0.48,
            "min_question_label_confidence": 0.65,
            "min_region_area_ratio": 0.001,
            "question_id_ocr_enabled": True,
            "merge_default_regions": True,
        }
    )
    result = LocalLayoutObserver(
        settings,
        _multi_manifest(),
        backend=backend,
        label_reader=reader,
    ).observe(image_path, page=1)
    assert result["accepted"] is False
    assert "unresolved_content_region" in result["audit"]["reasons"]


def test_local_layout_observer_accepts_unique_high_confidence_question(workspace_tmp_path) -> None:
    manifest, _ = _manifest(workspace_tmp_path)
    image_path = workspace_tmp_path / "page.png"
    _page(image_path)
    backend = FakeLayoutBackend(
        [
            {
                "label": "question_block",
                "score": 0.96,
                "coordinate": [5, 6, 116, 118],
                "question_label": "1．1．1",
                "question_label_confidence": 0.94,
            },
            {"label": "identity", "score": 0.99, "coordinate": [0, 0, 30, 8]},
        ]
    )
    result = LocalLayoutObserver(_settings(workspace_tmp_path), manifest, backend=backend).observe(
        image_path, page=1
    )
    assert result["accepted"] is True
    assert result["observation"]["questions"] == [
        {
            "question_id": "1.1.1",
            "bbox": [5, 6, 116, 118],
            "confidence": 0.94,
            "question_type": "question_block",
            "artifact_ref": str(image_path.resolve()),
        }
    ]
    assert result["audit"]["status"] == "accepted"
    assert backend.calls == 1


def test_local_layout_gate_rejects_unresolved_content(workspace_tmp_path) -> None:
    manifest, _ = _manifest(workspace_tmp_path)
    image_path = workspace_tmp_path / "page.png"
    _page(image_path)
    backend = FakeLayoutBackend(
        [
            {
                "label": "question_block",
                "score": 0.96,
                "coordinate": [5, 6, 116, 118],
            }
        ]
    )
    result = LocalLayoutObserver(_settings(workspace_tmp_path), manifest, backend=backend).observe(
        image_path, page=1
    )
    assert result["accepted"] is False
    assert "unresolved_content_region" in result["audit"]["reasons"]
    assert "no_resolved_question_ids" in result["audit"]["reasons"]


def test_local_layout_gate_rejects_low_confidence_content_candidate(workspace_tmp_path) -> None:
    manifest, _ = _manifest(workspace_tmp_path)
    image_path = workspace_tmp_path / "page.png"
    _page(image_path)
    backend = FakeLayoutBackend(
        [
            {
                "label": "question_block",
                "score": 0.96,
                "coordinate": [5, 6, 116, 70],
                "question_label": "1.1.1",
                "question_label_confidence": 0.94,
            },
            {
                "label": "student_answer",
                "score": 0.70,
                "coordinate": [5, 72, 116, 118],
            },
        ]
    )
    result = LocalLayoutObserver(_settings(workspace_tmp_path), manifest, backend=backend).observe(
        image_path, page=1
    )
    assert result["accepted"] is False
    assert "low_content_region_confidence" in result["audit"]["reasons"]


def test_pipeline_uses_local_layout_without_online_page_observer(workspace_tmp_path) -> None:
    _, manifest_path = _manifest(workspace_tmp_path)
    processed = workspace_tmp_path / "processed" / "student-1"
    processed.mkdir(parents=True)
    _page(processed / "page_1.png")
    backend = FakeLayoutBackend(
        [
            {
                "label": "question_block",
                "score": 0.97,
                "coordinate": [5, 5, 115, 115],
                "question_label": "1.1.1",
                "question_label_confidence": 0.96,
            }
        ]
    )
    provider = PreparationProvider()
    graph_input = build_student_graph_input(
        processed_student_dir=processed,
        answer_manifest_path=manifest_path,
        artifact_root=workspace_tmp_path / "artifacts",
        provider=provider,
        assignment_id="第一周",
        student_id="student-1",
        run_id="local-layout-run",
        budget=Budget(max_calls=4, max_input_tokens=10000, max_output_tokens=2000, max_image_pixels=1_000_000),
        local_layout_config={
            "enabled": True,
            "model_name": "PP-DocLayoutV3-test",
            "engine": "onnxruntime",
            "question_id_ocr_enabled": False,
        },
        local_layout_backend=backend,
    )
    assert not any("页面和题号观察器" in prompt for prompt in provider.prompts)
    assert any("忠实转写器" in prompt for prompt in provider.prompts)
    assert graph_input["layout_audit"][0]["status"] == "accepted"
    assert graph_input["question_jobs"]["1.1.1"]["route"] == "fast"
    assert any(item["stage"] == "local_layout_accepted" for item in graph_input["warnings"])


def test_pipeline_uses_default_model_rule_merge_before_online_observer(workspace_tmp_path) -> None:
    _, manifest_path = _manifest(workspace_tmp_path)
    processed = workspace_tmp_path / "processed" / "student-1"
    processed.mkdir(parents=True)
    _page(processed / "page_1.png")
    backend = FakeLayoutBackend(
        [
            {"label": "text", "score": 0.98, "coordinate": [5, 5, 115, 55]},
            {"label": "inline_formula", "score": 0.96, "coordinate": [8, 58, 112, 115]},
        ]
    )
    reader = FakeQuestionLabelReader(
        [{"text": "1.1.1", "confidence": 0.97, "bbox": [5, 5, 50, 20]}]
    )
    provider = PreparationProvider()
    graph_input = build_student_graph_input(
        processed_student_dir=processed,
        answer_manifest_path=manifest_path,
        artifact_root=workspace_tmp_path / "artifacts",
        provider=provider,
        assignment_id="第一周",
        student_id="student-1",
        run_id="default-layout-rule-merge-run",
        budget=Budget(max_calls=4, max_input_tokens=10000, max_output_tokens=2000, max_image_pixels=1_000_000),
        local_layout_config={
            "enabled": True,
            "model_name": "PP-DocLayoutV3",
            "engine": "onnxruntime",
            "min_region_confidence": 0.48,
            "min_question_label_confidence": 0.65,
            "question_id_ocr_enabled": True,
            "merge_default_regions": True,
        },
        local_layout_backend=backend,
        question_label_reader=reader,
    )
    assert not any("页面和题号观察器" in prompt for prompt in provider.prompts)
    assert graph_input["layout_audit"][0]["mode"] == "default_model_rule_merge"
    assert graph_input["layout_audit"][0]["status"] == "accepted"
    assert graph_input["question_jobs"]["1.1.1"]["route"] == "fast"


def test_pipeline_falls_back_online_when_local_gate_rejects(workspace_tmp_path) -> None:
    _, manifest_path = _manifest(workspace_tmp_path)
    processed = workspace_tmp_path / "processed" / "student-1"
    processed.mkdir(parents=True)
    _page(processed / "page_1.png")
    backend = FakeLayoutBackend(
        [{"label": "question_block", "score": 0.97, "coordinate": [5, 5, 115, 115]}]
    )
    provider = PreparationProvider()
    graph_input = build_student_graph_input(
        processed_student_dir=processed,
        answer_manifest_path=manifest_path,
        artifact_root=workspace_tmp_path / "artifacts",
        provider=provider,
        assignment_id="第一周",
        student_id="student-1",
        run_id="local-layout-fallback-run",
        budget=Budget(max_calls=4, max_input_tokens=10000, max_output_tokens=2000, max_image_pixels=1_000_000),
        local_layout_config={
            "enabled": True,
            "model_name": "PP-DocLayoutV3-test",
            "engine": "onnxruntime",
            "question_id_ocr_enabled": False,
        },
        local_layout_backend=backend,
    )
    assert any("页面和题号观察器" in prompt for prompt in provider.prompts)
    assert graph_input["layout_audit"][0]["status"] == "rejected"
    assert any(item["stage"] == "local_layout_online_fallback" for item in graph_input["warnings"])


def test_pipeline_falls_back_online_when_local_model_is_not_deployed(workspace_tmp_path) -> None:
    _, manifest_path = _manifest(workspace_tmp_path)
    processed = workspace_tmp_path / "processed" / "student-1"
    processed.mkdir(parents=True)
    _page(processed / "page_1.png")
    provider = PreparationProvider()
    missing_model_dir = workspace_tmp_path / "models" / "PP-DocLayoutV3"
    graph_input = build_student_graph_input(
        processed_student_dir=processed,
        answer_manifest_path=manifest_path,
        artifact_root=workspace_tmp_path / "artifacts",
        provider=provider,
        assignment_id="第一周",
        student_id="student-1",
        run_id="local-layout-unavailable-run",
        budget=Budget(max_calls=4, max_input_tokens=10000, max_output_tokens=2000, max_image_pixels=1_000_000),
        local_layout_config={
            "enabled": True,
            "model_name": "PP-DocLayoutV3",
            "model_dir": str(missing_model_dir),
            "engine": "onnxruntime",
            "allow_model_download": False,
            "question_id_ocr_enabled": False,
        },
    )
    assert any("页面和题号观察器" in prompt for prompt in provider.prompts)
    assert graph_input["layout_audit"][0]["status"] == "unavailable"
    assert any(
        item["stage"] == "local_layout_unavailable_online_fallback"
        for item in graph_input["warnings"]
    )
