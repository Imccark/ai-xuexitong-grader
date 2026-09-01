from __future__ import annotations

import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import review_app
from project_config import AssignmentConfig, SubjectConfig
from grading_graph.review import ReviewStore
from grading_graph.schemas import CandidateResult, EvidenceRef, QuestionResult
from grading_graph.store import atomic_write_json


def _assignment(tmp_path: Path) -> AssignmentConfig:
    week_dir = tmp_path / "第一周"
    processed = week_dir / "processed_images"
    results = week_dir / "results"
    (processed / "student-1").mkdir(parents=True)
    results.mkdir(parents=True)
    subject = SubjectConfig(
        subject_id="linear_algebra",
        subject_name="线性代数",
        model="fake",
        base_url="http://fake",
        api_key_env="DASHSCOPE_API_KEY",
        prompt_template_path=tmp_path / "prompt.txt",
        grading_requirements="",
        output_format="",
    )
    return AssignmentConfig(
        assignment_id="第一周",
        week_name="第一周",
        week_dir=week_dir,
        raw_submissions_dir=week_dir / "raw_submissions",
        processed_images_dir=processed,
        results_dir=results,
        answer_key_path=week_dir / "answer.tex",
        preprocess_summary_path=week_dir / "preprocess_summary.txt",
        grading_summary_path=week_dir / "summary.txt",
        subject=subject,
    )


def _candidate() -> CandidateResult:
    return CandidateResult(
        graph_version="test",
        run_id="run-1",
        assignment_id="第一周",
        student_id="student-1",
        status="review_required",
        overall="partial",
        unresolved_risk_count=1,
        question_results={
            "1.1.1": QuestionResult(
                question_id="1.1.1",
                verdict="partial",
                confidence=0.7,
                needs_verification=True,
                risk_level="high",
                evidence_refs=[EvidenceRef(span_id="span-1", page=1, bbox=(1, 1, 10, 10), artifact_ref="page_1.png")],
            )
        },
    )


def _request(server, method: str, path: str, payload: dict | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    body = json.dumps(payload or {}, ensure_ascii=False) if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = json.loads(response.read().decode("utf-8"))
    connection.close()
    return response.status, data


def test_review_http_api_is_agent_read_only_and_rejects_manual_mutation(workspace_tmp_path) -> None:
    assignment = _assignment(workspace_tmp_path)
    repository = review_app.ReviewRepository(assignment)
    repository.review_store.save_candidate(_candidate())
    previous = review_app._repository
    review_app._repository = repository
    http_server = ThreadingHTTPServer(("127.0.0.1", 0), review_app.create_handler())
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        status, queue = _request(http_server, "GET", "/api/review-queue")
        assert status == 200
        assert queue["queue"][0]["status"] == "review_required"
        assert queue["queue"][0]["readOnly"] is True

        for path in (
            "/api/review/student-1/question/1.1.1/rerun",
            "/api/review/student-1/question/1.1.1/decision",
            "/api/review/student-1/finalize",
            "/api/review/student-1/reopen",
            "/api/review/student-1/submit",
            "/api/gold/sample/assignment/hash",
            "/api/student/student-1",
        ):
            status, payload = _request(http_server, "POST", path, {})
            assert status == 410
            assert payload["error"] == "manual_scoring_disabled"
    finally:
        http_server.shutdown()
        http_server.server_close()
        review_app._repository = previous


def test_review_console_grading_command_uses_formal_agent_engine_and_budgets() -> None:
    assignment_path = Path("configs/assignments/第一周.json").resolve()
    command = review_app._build_pipeline_command("grading", assignment_path, 4, False)

    assert command[1:4] == ["-u", "run_batch_grading.py", "--assignment"]
    assert command[command.index("--engine") + 1] == "candidate"
    assert "--online" in command
    assert int(command[command.index("--max-students") + 1]) >= 1
    assert int(command[command.index("--max-calls") + 1]) > 0
    assert int(command[command.index("--max-input-tokens") + 1]) > 0
    assert int(command[command.index("--max-output-tokens") + 1]) > 0


def test_api_key_read_reports_presence_without_echoing_secret(monkeypatch) -> None:
    monkeypatch.setattr(review_app, "get_local_env_var", lambda _env_name: "provider-secret-value")
    http_server = ThreadingHTTPServer(("127.0.0.1", 0), review_app.create_handler())
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _request(http_server, "GET", "/api/apikey?env=DASHSCOPE_API_KEY")
        assert status == 200
        assert payload["hasApiKey"] is True
        assert "apiKey" not in payload
        assert "provider-secret-value" not in json.dumps(payload, ensure_ascii=False)
    finally:
        http_server.shutdown()
        http_server.server_close()


def test_review_repository_exposes_hashed_agent_image_variants_safely(workspace_tmp_path) -> None:
    assignment = _assignment(workspace_tmp_path)
    page_path = assignment.processed_images_dir / "student-1" / "page_1.png"
    page_path.write_bytes(b"placeholder")
    artifact_page = assignment.week_dir / "agent_artifacts" / ReviewStore.student_hash("student-1") / "pages" / "page_1"
    artifact_page.mkdir(parents=True)
    normalized = artifact_page / "normalized.png"
    rectified = artifact_page / "rectified.png"
    normalized.write_bytes(b"normalized")
    rectified.write_bytes(b"rectified")
    repository = review_app.ReviewRepository(assignment)

    payload = repository.get_student_payload("student-1")
    assert payload["imageVariants"][0]["normalized"] == "/agent-images/student-1/1/normalized.png"
    assert payload["imageVariants"][0]["rectified"] == "/agent-images/student-1/1/rectified.png"
    assert repository.resolve_agent_image("student-1", "1", "normalized.png") == normalized.resolve()
    assert repository.resolve_agent_image("student-1", "1", "rectified.png") == rectified.resolve()
    with pytest.raises(FileNotFoundError):
        repository.resolve_agent_image("student-1", "1", "..\\..\\results\\student-1.json")
    with pytest.raises(FileNotFoundError):
        repository.resolve_image("..", "results\\student-1.json")


def test_review_repository_serializes_concurrent_writes_with_revision_conflict(workspace_tmp_path) -> None:
    repository = review_app.ReviewRepository(_assignment(workspace_tmp_path))
    repository.review_store.save_candidate(_candidate())

    def write_decision():
        try:
            return repository.record_review_decision(
                "student-1",
                {"questionId": "1.1.1", "action": "accept", "expectedRevision": 1},
            )
        except Exception as exc:  # one writer must lose the optimistic-lock race
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: write_decision(), range(2)))
    assert sum(not isinstance(result, review_app.ReviewConflict) for result in results) == 1
    assert sum(isinstance(result, review_app.ReviewConflict) for result in results) == 1
    assert repository.review_store.snapshot("student-1")["revision"] == 2


def _rerun_manifest(root: Path) -> Path:
    slices = root / "reference_slices"
    slices.mkdir(parents=True)
    (slices / "answer.tex").write_text("x=1", encoding="utf-8")
    path = root / "manifest.json"
    atomic_write_json(
        path,
        {
            "assignment_id": "第一周",
            "answer_hash": "a" * 64,
            "compiler_version": "test",
            "questions": {
                "1.1.1": {
                    "question_id": "1.1.1",
                    "artifact_ref": "reference_slices/answer.tex",
                    "sha256": "b" * 64,
                    "character_count": 3,
                }
            },
        },
    )
    return path


def test_review_repository_rerun_replaces_only_target_and_preserves_audit(workspace_tmp_path) -> None:
    assignment = _assignment(workspace_tmp_path)
    (assignment.processed_images_dir / "student-1" / "page_1.png").write_bytes(b"placeholder")
    repository = review_app.ReviewRepository(assignment)
    repository.review_store.save_candidate(_candidate())
    manifest_path = _rerun_manifest(workspace_tmp_path / "manifest")

    class RerunProvider:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete_json(self, prompt, schema, image_ref=None):
            self.calls.append(prompt)
            if "targeted verifier" in prompt:
                return {"decisive": True, "verdict": "correct", "reason": "targeted"}
            return {"verdict": "correct", "confidence": 0.95, "needs_verification": True, "risk_level": "high"}

    provider = RerunProvider()
    repository._compiled_manifest_path = lambda: manifest_path
    repository._rerun_provider = lambda: provider
    result = repository.rerun_review_question("student-1", "1.1.1", {"expectedRevision": 1})

    assert result["rerun"]["status"] == "completed"
    assert result["candidate"]["question_results"]["1.1.1"]["verdict"] == "correct"
    assert result["candidate"]["unresolved_risk_count"] == 0
    assert len(provider.calls) == 2
    assert all("1.1.1" in prompt for prompt in provider.calls)
    assert result["decisions"][-1]["rerun_run_id"] == result["candidate"]["run_id"]
    assert (assignment.week_dir / "agent_artifacts" / ReviewStore.student_hash("student-1") / "rerun_audit.json").is_file()


def test_review_repository_rerun_failure_keeps_old_candidate_and_records_type(workspace_tmp_path) -> None:
    assignment = _assignment(workspace_tmp_path)
    repository = review_app.ReviewRepository(assignment)
    original = _candidate()
    repository.review_store.save_candidate(original)
    manifest_path = _rerun_manifest(workspace_tmp_path / "manifest")

    class FailingProvider:
        def complete_json(self, prompt, schema, image_ref=None):
            raise TimeoutError("provider detail must not be persisted")

    repository._compiled_manifest_path = lambda: manifest_path
    repository._rerun_provider = lambda: FailingProvider()
    result = repository.rerun_review_question("student-1", "1.1.1", {"expectedRevision": 1})

    assert result["rerun"]["status"] == "failed"
    assert result["rerun"]["errorType"] == "GradingProviderError"
    assert repository.review_store.load_candidate("student-1").run_id == original.run_id
    assert "provider detail" not in json.dumps(result, ensure_ascii=False)
    assert result["decisions"][-1]["action"] == "rerun"
