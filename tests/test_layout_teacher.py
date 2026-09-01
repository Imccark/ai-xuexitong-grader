from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from PIL import Image

from evaluation.layout_teacher import (
    CONSENSUS_VERSION,
    LABELING_VERSION,
    QUALITY_VERSION,
    OnlineBudget,
    RelayLayoutTeacher,
    _acquire_page_lock,
    _maximum_weight_pairs,
    bbox_coverage,
    bbox_iou,
    build_pilot_manifest,
    decode_layout_response,
    infer_quality_verdict_from_layout,
    compare_passes,
    label_manifest,
    layout_quality_flags,
    merge_consensus_layout,
    parse_layout_content,
    parse_quality_verdict_content,
    prompt_for_pass,
    public_manifest_row,
    resolve_persistent_quality_decisions,
    sanitize_layout,
    validate_layout,
)
from run_teacher_labeling import migrate_quality_metadata, pending_manifest_rows
from evaluation.reconcile_layout_labels import reconcile_result


def _layout(bbox=None):
    return {
        "rotation_degrees_clockwise": 0,
        "image_quality": {"blur": "none", "perspective": "none", "exposure": "normal"},
        "regions": [
            {
                "region_id": "r1",
                "region_type": "student_answer",
                "bbox": bbox or [0.1, 0.1, 0.9, 0.9],
                "reading_order": 0,
                "question_label": "1(1)",
                "parent_region_id": "",
                "continues_from_previous_page": False,
                "continues_to_next_page": False,
                "contains_critical_minus": True,
                "confidence": 0.95,
                "review_notes": "",
            }
        ],
        "page_notes": "",
    }


def test_validate_and_consensus() -> None:
    proposal = validate_layout(_layout())
    critic = validate_layout(_layout([0.11, 0.1, 0.89, 0.9]))
    result = compare_passes(proposal, critic, minimum_iou=0.85)
    assert result["status"] == "high_confidence_silver"
    assert result["minimum_matched_iou"] > 0.9


def test_invalid_bbox_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive normalized"):
        validate_layout(_layout([0.8, 0.1, 0.2, 0.9]))


def test_sanitize_layout_clears_orphan_parent_links() -> None:
    layout = _layout()
    layout["regions"][0].update(
        {
            "region_type": "subquestion",
            "parent_region_id": "question-from-another-page",
        }
    )

    sanitized = sanitize_layout(layout)

    assert sanitized["regions"][0]["parent_region_id"] == ""


def test_layout_quality_flags_small_labeled_region_next_to_large_content() -> None:
    layout = _layout([0.1, 0.1, 0.3, 0.13])
    layout["regions"][0].update({"region_type": "subquestion", "question_label": "(3)"})
    layout["regions"].append(
        {
            **layout["regions"][0],
            "region_id": "r2",
            "bbox": [0.1, 0.14, 0.9, 0.5],
            "question_label": "",
            "reading_order": 1,
        }
    )

    flags = layout_quality_flags(layout)

    assert flags == [
        {
            "kind": "answerless_label_candidate",
            "region_id": "r1",
            "question_label": "(3)",
            "area": 0.006,
            "neighbor_region_id": "r2",
        }
    ]


def test_bbox_iou() -> None:
    assert bbox_iou([0, 0, 1, 1], [0, 0, 1, 1]) == 1
    assert bbox_iou([0, 0, 0.2, 0.2], [0.8, 0.8, 1, 1]) == 0


def test_budget_stops_before_extra_call() -> None:
    budget = OnlineBudget(max_calls=1, max_input_tokens=100, max_output_tokens=100)
    budget.calls = 1
    with pytest.raises(RuntimeError, match="call budget"):
        budget.check_before()


def test_budget_reserves_hidden_reasoning_tokens() -> None:
    budget = OnlineBudget(max_calls=2, max_input_tokens=100, max_output_tokens=6500, output_tokens=1000)
    with pytest.raises(RuntimeError, match="output-token budget"):
        budget.check_before(reserve_output_tokens=6000)


def test_budget_reservation_prevents_concurrent_oversubscription() -> None:
    budget = OnlineBudget(max_calls=1, max_input_tokens=100, max_output_tokens=10_000)
    budget.reserve_call(reserve_output_tokens=6000)
    with pytest.raises(RuntimeError, match="call budget"):
        budget.reserve_call(reserve_output_tokens=1000)
    budget.cancel_call(reserve_output_tokens=6000)
    budget.reserve_call(reserve_output_tokens=1000)
    budget.cancel_call(reserve_output_tokens=1000)


def test_teacher_prefers_project_local_key_over_stale_process_key(monkeypatch) -> None:
    captured = {}

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setenv("SOL_ANNOTATION_API_KEY", "stale-process-key")
    monkeypatch.setattr("evaluation.layout_teacher.get_local_env_var", lambda _name: "project-local-key")
    monkeypatch.setattr("evaluation.layout_teacher._dead_loopback_proxy_sentinel", lambda: False)
    monkeypatch.setattr("openai.OpenAI", fake_openai)

    teacher = RelayLayoutTeacher.from_config(
        {
            "provider": {
                "api_key_env": "SOL_ANNOTATION_API_KEY",
                "base_url": "https://relay.example/v1",
                "model": "gpt-5.6-sol",
            },
            "pilot": {"max_retries_per_call": 0},
        },
        budget=OnlineBudget(max_calls=1, max_input_tokens=1000, max_output_tokens=1000),
    )

    assert captured["api_key"] == "project-local-key"
    assert teacher.model == "gpt-5.6-sol"


def test_parallel_independent_passes_share_one_page_result(workspace_tmp_path) -> None:
    image_path = workspace_tmp_path / "page.png"
    Image.new("RGB", (120, 160), "white").save(image_path)
    image_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
    barrier = threading.Barrier(2)

    class FakeTeacher:
        model = "gpt-5.6-sol"
        budget = OnlineBudget(max_calls=4, max_input_tokens=1000, max_output_tokens=20_000)

        def label(self, _image_path, *, pass_name, context=None):
            assert context is None
            barrier.wait(timeout=2)
            return _layout(), {
                "labeling_version": LABELING_VERSION,
                "request_model": self.model,
                "prompt_sha256": "unused-in-new-result",
                "schema_sha256": "unused-in-new-result",
            }

    report = label_manifest(
        [
            {
                "page_id": "a" * 64,
                "image_sha256": image_sha,
                "source_path": str(image_path),
                "width": 120,
                "height": 160,
            }
        ],
        FakeTeacher(),
        output_dir=workspace_tmp_path / "results",
        max_pages=1,
        pass_workers=2,
    )

    assert report["completed_pages"] == 1
    assert report["failed_pages"] == 0
    assert report["pass_workers"] == 2


def test_existing_suspicious_result_runs_only_quality_repair(workspace_tmp_path) -> None:
    image_path = workspace_tmp_path / "page.png"
    Image.new("RGB", (120, 160), "white").save(image_path)
    image_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
    page_id = "b" * 64
    output = workspace_tmp_path / "results"
    output.mkdir()
    suspicious = _layout([0.1, 0.1, 0.3, 0.13])
    suspicious["regions"][0].update({"region_type": "subquestion", "question_label": "(3)"})
    suspicious["regions"].append(
        {
            **suspicious["regions"][0],
            "region_id": "r2",
            "bbox": [0.1, 0.14, 0.9, 0.5],
            "question_label": "",
            "reading_order": 1,
        }
    )
    (output / f"{page_id}.json").write_text(
        json.dumps(
            {
                "page_id": page_id,
                "image_sha256": image_sha,
                "teacher": {"labeling_version": LABELING_VERSION, "consensus_version": CONSENSUS_VERSION},
                "consensus": {"status": "adjudicated_silver"},
                "final_layout": suspicious,
            }
        ),
        encoding="utf-8",
    )
    calls = []

    class FakeTeacher:
        model = "gpt-5.6-sol"
        budget = OnlineBudget(max_calls=2, max_input_tokens=1000, max_output_tokens=20_000)

        def label(self, _image_path, *, pass_name, context=None):
            calls.append((pass_name, context))
            repaired = _layout([0.1, 0.14, 0.9, 0.5])
            repaired["regions"][0].update({"region_type": "subquestion", "question_label": "(3)"})
            return repaired, {
                "labeling_version": LABELING_VERSION,
                "request_model": self.model,
                "reported_model": self.model,
                "prompt_sha256": "repair",
                "schema_sha256": "schema",
            }

    report = label_manifest(
        [{"page_id": page_id, "image_sha256": image_sha, "source_path": str(image_path)}],
        FakeTeacher(),
        output_dir=output,
        max_pages=1,
    )

    saved = json.loads((output / f"{page_id}.json").read_text(encoding="utf-8"))
    assert [name for name, _context in calls] == ["repair"]
    assert report["completed_pages"] == 1
    assert saved["teacher"]["quality_version"] == QUALITY_VERSION
    assert saved["consensus"]["quality_repair_applied"] is True
    assert saved["consensus"]["quality_flags_after_repair"] == []


def test_existing_repaired_persistent_candidate_runs_only_verifier(workspace_tmp_path) -> None:
    image_path = workspace_tmp_path / "page.png"
    Image.new("RGB", (120, 160), "white").save(image_path)
    image_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
    page_id = "c" * 64
    output = workspace_tmp_path / "results"
    output.mkdir()
    suspicious = _layout([0.1, 0.1, 0.3, 0.13])
    suspicious["regions"][0].update({"region_type": "subquestion", "question_label": "(3)"})
    suspicious["regions"].append(
        {
            **suspicious["regions"][0],
            "region_id": "r2",
            "bbox": [0.1, 0.14, 0.9, 0.5],
            "question_label": "",
            "reading_order": 1,
        }
    )
    (output / f"{page_id}.json").write_text(
        json.dumps(
            {
                "page_id": page_id,
                "image_sha256": image_sha,
                "teacher": {
                    "labeling_version": LABELING_VERSION,
                    "consensus_version": CONSENSUS_VERSION,
                    "quality_version": "layout-quality-v1-answerless-label-guard",
                    "repair": {"reported_model": "gpt-5.6-sol"},
                },
                "consensus": {"status": "adjudicated_silver", "quality_repair_applied": True},
                "repair": suspicious,
                "final_layout": suspicious,
            }
        ),
        encoding="utf-8",
    )
    calls = []

    class FakeTeacher:
        model = "gpt-5.6-sol"
        budget = OnlineBudget(max_calls=2, max_input_tokens=1000, max_output_tokens=20_000)

        def label(self, *_args, **_kwargs):
            raise AssertionError("proposal/repair must not run")

        def verify_quality(self, _image_path, *, pass_name, context, expected_region_ids):
            calls.append((pass_name, context, expected_region_ids))
            return {
                "verifier_version": "layout-quality-verifier-v1",
                "decisions": [
                    {
                        "region_id": "r1",
                        "decision": "keep",
                        "reason": "meaningful_work",
                        "confidence": 0.95,
                    }
                ],
            }, {
                "labeling_version": LABELING_VERSION,
                "request_model": self.model,
                "reported_model": self.model,
                "prompt_sha256": "verifier",
                "schema_sha256": "schema",
            }

    report = label_manifest(
        [{"page_id": page_id, "image_sha256": image_sha, "source_path": str(image_path)}],
        FakeTeacher(),
        output_dir=output,
        max_pages=1,
    )

    saved = json.loads((output / f"{page_id}.json").read_text(encoding="utf-8"))
    assert [item[0] for item in calls] == ["quality_verifier"]
    assert report["quality"]["verified_pages"] == 1
    assert saved["training_eligible"] is True
    assert saved["consensus"]["quality_resolution"] == "verified"
    assert saved["consensus"]["confirmed_retained_quality_flags"][0]["region_id"] == "r1"


def test_page_workers_process_multiple_pages_concurrently(workspace_tmp_path) -> None:
    active = 0
    peak = 0
    lock = threading.Lock()
    manifest = []
    for index in range(4):
        image_path = workspace_tmp_path / f"page-{index}.png"
        Image.new("RGB", (120, 160), "white").save(image_path)
        manifest.append(
            {
                "page_id": f"{index + 1:064x}",
                "image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                "source_path": str(image_path),
            }
        )

    class FakeTeacher:
        model = "gpt-5.6-sol"
        budget = OnlineBudget(max_calls=20, max_input_tokens=1000, max_output_tokens=100_000)
        max_request_concurrency = 4

        def label(self, _image_path, *, pass_name, context=None):
            nonlocal active, peak
            assert context is None
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return _layout(), {
                "labeling_version": LABELING_VERSION,
                "request_model": self.model,
                "reported_model": self.model,
                "prompt_sha256": pass_name,
                "schema_sha256": "schema",
            }

    report = label_manifest(
        manifest,
        FakeTeacher(),
        output_dir=workspace_tmp_path / "parallel-results",
        max_pages=4,
        pass_workers=1,
        page_workers=4,
    )

    assert report["completed_pages"] == 4
    assert report["failed_pages"] == 0
    assert report["page_workers"] == 4
    assert peak == 4


def test_teacher_recovers_from_reasoning_only_response(workspace_tmp_path) -> None:
    image_path = workspace_tmp_path / "page.png"
    Image.new("RGB", (120, 160), "white").save(image_path)
    compact = {
        "rotation": 0,
        "regions": [
            {"id": "q1", "type": "question_block", "box": [0.1, 0.1, 0.9, 0.8], "order": 1, "label": "1", "parent": "", "prev": False, "next": False, "minus": False, "confidence": 0.9}
        ],
    }
    responses = [
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=" "), finish_reason="length")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=100),
            model="gpt-5.6-sol",
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(compact)), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=50),
            model="gpt-5.6-sol",
        ),
    ]
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)

    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    teacher = RelayLayoutTeacher(
        client,
        model="gpt-5.6-sol",
        budget=OnlineBudget(max_calls=4, max_input_tokens=1000, max_output_tokens=20_000),
    )
    layout, _meta = teacher.label(image_path, pass_name="proposal")
    assert layout["regions"][0]["region_id"] == "q1"
    assert [call["max_tokens"] for call in calls] == [3600, 6000]
    assert all(call["reasoning_effort"] == "low" for call in calls)


def test_teacher_retries_transient_connection_failure_once(workspace_tmp_path) -> None:
    image_path = workspace_tmp_path / "page.png"
    Image.new("RGB", (120, 160), "white").save(image_path)
    compact = {
        "rotation": 0,
        "regions": [
            {"id": "q1", "type": "question_block", "box": [0.1, 0.1, 0.9, 0.8], "order": 1, "label": "1", "parent": "", "prev": False, "next": False, "minus": False, "confidence": 0.9}
        ],
    }

    class APIConnectionError(Exception):
        pass

    class FakeCompletions:
        attempts = 0

        def create(self, **_kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise APIConnectionError("Connection error.")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(compact)), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
                model="gpt-5.6-sol",
            )

    completions = FakeCompletions()
    teacher = RelayLayoutTeacher(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="gpt-5.6-sol",
        budget=OnlineBudget(max_calls=2, max_input_tokens=1000, max_output_tokens=20_000),
        max_retries_per_call=1,
    )

    layout, _meta = teacher.label(image_path, pass_name="proposal")

    assert layout["regions"][0]["region_id"] == "q1"
    assert completions.attempts == 2
    assert teacher.request_attempts == 2
    assert teacher.transient_retries == 1
    assert teacher.transient_failures == 1
    assert teacher.budget.calls == 1


def test_global_request_semaphore_caps_parallel_calls(workspace_tmp_path) -> None:
    image_path = workspace_tmp_path / "page.png"
    Image.new("RGB", (120, 160), "white").save(image_path)
    compact = {"rotation": 0, "regions": []}

    class FakeCompletions:
        active = 0
        peak = 0
        lock = threading.Lock()

        def create(self, **_kwargs):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.03)
            with self.lock:
                self.active -= 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(compact)), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                model="gpt-5.6-sol",
            )

    completions = FakeCompletions()
    teacher = RelayLayoutTeacher(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="gpt-5.6-sol",
        budget=OnlineBudget(max_calls=8, max_input_tokens=1000, max_output_tokens=40_000),
        max_request_concurrency=3,
        max_retries_per_call=0,
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(teacher.label, image_path, pass_name="proposal") for _ in range(8)]
        for future in futures:
            future.result()

    assert completions.peak == 3


def test_quality_verdict_requires_every_expected_region() -> None:
    content = json.dumps(
        {
            "decisions": [
                {"id": "r1", "decision": "keep", "reason": "meaningful_work", "confidence": 0.93}
            ]
        }
    )
    verdict = parse_quality_verdict_content(content, expected_region_ids={"r1"})
    assert verdict["decisions"][0]["region_id"] == "r1"
    with pytest.raises(ValueError, match="every expected"):
        parse_quality_verdict_content(content, expected_region_ids={"r1", "r2"})
    unwrapped = parse_quality_verdict_content(
        json.dumps({"id": "r1", "decision": "remove", "reason": "bare_label", "confidence": 0.97}),
        expected_region_ids={"r1"},
    )
    assert unwrapped["decisions"][0]["decision"] == "remove"


def test_quality_tiebreaker_can_infer_vote_from_full_layout_fallback() -> None:
    candidate = _layout([0.1, 0.1, 0.3, 0.13])
    candidate["regions"][0].update({"region_type": "subquestion", "question_label": "(3)"})
    parsed = parse_quality_verdict_content(
        json.dumps({"rotation": 0, "regions": []}),
        expected_region_ids={"r1"},
        allow_layout_fallback=True,
    )
    verdict = infer_quality_verdict_from_layout(
        candidate,
        parsed["layout_fallback"],
        [{"region_id": "r1"}],
    )
    assert verdict["transport_fallback"] == "complete_layout"
    assert verdict["decisions"][0]["decision"] == "remove"


def test_persistent_quality_vote_resolution() -> None:
    first = {
        "decisions": [
            {"region_id": "keep", "decision": "keep"},
            {"region_id": "remove", "decision": "remove"},
            {"region_id": "uncertain", "decision": "uncertain"},
        ]
    }
    second = {
        "decisions": [
            {"region_id": "remove", "decision": "remove"},
            {"region_id": "uncertain", "decision": "remove"},
        ]
    }
    resolution = resolve_persistent_quality_decisions(first, second)
    assert resolution["kept_region_ids"] == ["keep"]
    assert resolution["removed_region_ids"] == ["remove"]
    assert resolution["unresolved_region_ids"] == ["uncertain"]
    assert resolution["training_eligible"] is False


def test_page_lock_prevents_concurrent_duplicate(workspace_tmp_path) -> None:
    lock = workspace_tmp_path / "locks" / "page.lock"
    assert _acquire_page_lock(lock) is True
    assert _acquire_page_lock(lock) is False


def test_page_lock_reclaims_dead_owner(workspace_tmp_path) -> None:
    lock = workspace_tmp_path / "locks" / "abandoned.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": 999_999_999, "created_unix": 0}), encoding="utf-8")
    assert _acquire_page_lock(lock) is True
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_maximum_weight_matching_avoids_order_greedy_pairing(monkeypatch) -> None:
    left = [_layout([0.0, 0.0, 0.1, 0.1])["regions"][0], _layout([0.1, 0.0, 0.2, 0.1])["regions"][0]]
    right = [_layout([0.0, 0.1, 0.1, 0.2])["regions"][0], _layout([0.1, 0.1, 0.2, 0.2])["regions"][0]]
    weights = {
        (tuple(left[0]["bbox"]), tuple(right[0]["bbox"])): 0.80,
        (tuple(left[0]["bbox"]), tuple(right[1]["bbox"])): 0.70,
        (tuple(left[1]["bbox"]), tuple(right[0]["bbox"])): 0.75,
        (tuple(left[1]["bbox"]), tuple(right[1]["bbox"])): 0.10,
    }
    monkeypatch.setattr(
        "evaluation.layout_teacher.bbox_iou",
        lambda first, second: weights[(tuple(first), tuple(second))],
    )
    assert _maximum_weight_pairs(left, right) == {0: (1, 0.70), 1: (0, 0.75)}


def test_bbox_coverage_uses_union_without_double_counting() -> None:
    assert bbox_coverage([0.0, 0.0, 1.0, 1.0], [[0.0, 0.0, 0.6, 1.0], [0.4, 0.0, 1.0, 1.0]]) == pytest.approx(1.0)


def test_fragment_split_merges_without_adjudication() -> None:
    proposal = _layout([0.1, 0.1, 0.9, 0.9])
    proposal["regions"][0].update({"region_type": "question_block", "question_label": "4"})
    critic = _layout([0.1, 0.1, 0.5, 0.9])
    critic["regions"][0].update({"region_type": "question_block", "question_label": "4"})
    critic["regions"].append({**critic["regions"][0], "region_id": "r2", "bbox": [0.5, 0.1, 0.9, 0.9]})
    consensus = compare_passes(proposal, critic)
    assert consensus["status"] == "high_confidence_silver"
    assert consensus["reconciliation_mode"] == "fragment_coverage"
    merged = merge_consensus_layout(proposal, critic, consensus)
    assert [item["bbox"] for item in merged["regions"]] == [[0.1, 0.1, 0.5, 0.9], [0.5, 0.1, 0.9, 0.9]]


def test_geometry_only_consensus_drops_conflicting_question_label() -> None:
    proposal = _layout([0.1, 0.1, 0.9, 0.4])
    critic = _layout([0.11, 0.1, 0.89, 0.41])
    proposal["regions"][0].update({"region_type": "question_block", "question_label": "4.3.7"})
    critic["regions"][0].update({"region_type": "question_block", "question_label": "4,3.7"})
    consensus = compare_passes(proposal, critic)
    assert consensus["reconciliation_mode"] == "geometry_only"
    assert consensus["label_disagreement_count"] == 1
    merged = merge_consensus_layout(proposal, critic, consensus)
    assert merged["regions"][0]["question_label"] == ""


def test_reconcile_preserves_quality_repair_result() -> None:
    proposal = _layout([0.1, 0.1, 0.9, 0.9])
    critic = _layout([0.11, 0.1, 0.89, 0.9])
    repair = _layout([0.12, 0.12, 0.88, 0.88])
    reconciled, _resolved = reconcile_result(
        {
            "proposal": proposal,
            "critic": critic,
            "repair": repair,
            "teacher": {"quality_version": QUALITY_VERSION},
            "consensus": {
                "status": "high_confidence_silver",
                "quality_repair_applied": True,
                "quality_flags_before_repair": [{"kind": "answerless_label_candidate"}],
                "quality_flags_after_repair": [],
            },
            "final_layout": proposal,
        }
    )

    assert reconciled["final_layout"] == repair
    assert reconciled["repair"] == repair
    assert reconciled["consensus"]["quality_repair_applied"] is True
    assert reconciled["consensus"]["quality_flags_after_repair"] == []


def test_boundary_union_accepts_contained_box_variation() -> None:
    proposal = _layout([0.1, 0.1, 0.9, 0.5])
    critic = _layout([0.1, 0.1, 0.52, 0.5])
    for index, bbox in enumerate(([0.1, 0.55, 0.9, 0.7], [0.1, 0.75, 0.9, 0.9]), start=2):
        proposal["regions"].append({**proposal["regions"][0], "region_id": f"p{index}", "bbox": list(bbox)})
        critic["regions"].append({**critic["regions"][0], "region_id": f"c{index}", "bbox": list(bbox)})
    consensus = compare_passes(proposal, critic)
    assert consensus["reconciliation_mode"] == "boundary_union"
    merged = merge_consensus_layout(proposal, critic, consensus)
    assert merged["regions"][0]["bbox"] == [0.1, 0.1, 0.9, 0.5]


def test_offline_reconciliation_removes_unneeded_adjudicator() -> None:
    proposal = _layout([0.1, 0.1, 0.9, 0.4])
    critic = _layout([0.11, 0.1, 0.89, 0.41])
    original = {
        "page_id": "p1",
        "teacher": {"adjudicator": {"reported_model": "test"}},
        "proposal": proposal,
        "critic": critic,
        "adjudicator": _layout(),
        "consensus": {"status": "adjudicated_silver"},
        "final_layout": _layout(),
    }
    reconciled, resolved = reconcile_result(original)
    assert resolved is True
    assert reconciled["adjudicator"] is None
    assert reconciled["teacher"]["adjudicator"] is None
    assert reconciled["consensus"]["status"] == "high_confidence_silver"


def test_adjudicator_prompt_hash_changes_with_proposals() -> None:
    first = prompt_for_pass("adjudicator", {"proposal_a": _layout(), "proposal_b": _layout()})
    second = prompt_for_pass("adjudicator", {"proposal_a": _layout([0.2, 0.2, 0.8, 0.8]), "proposal_b": _layout()})
    assert first != second


def test_manifest_is_deterministic_and_public_row_has_no_path(workspace_tmp_path) -> None:
    for week, student in (("第一周", "student-a"), ("第二周", "student-b")):
        folder = workspace_tmp_path / week / "processed_images" / student
        folder.mkdir(parents=True)
        Image.new("RGB", (100, 120), "white").save(folder / "page_1.png")
    first = build_pilot_manifest(workspace_tmp_path, max_pages=2)
    second = build_pilot_manifest(workspace_tmp_path, max_pages=2)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert {row["assignment_id"] for row in first} == {"第一周", "第二周"}
    assert "source_path" not in public_manifest_row(first[0])
    assert len(first[0]["student_hash"]) == 64


def test_compact_transport_decodes_and_tiny_decoration_is_removed() -> None:
    decoded = decode_layout_response(
        {
            "rotation": 0,
            "regions": [
                {"id": "q1", "type": "question_block", "box": [0.1, 0.1, 0.9, 0.5], "order": 1, "label": "1", "parent": "", "prev": False, "next": False, "minus": True, "confidence": 0.95},
                {"id": "dust", "type": "header_footer", "box": [0.01, 0.01, 0.03, 0.02], "order": 0, "label": "", "parent": "", "prev": False, "next": False, "minus": False, "confidence": 0.7},
            ],
        }
    )
    cleaned = sanitize_layout(decoded)
    assert [item["region_id"] for item in cleaned["regions"]] == ["q1"]
    assert cleaned["regions"][0]["contains_critical_minus"] is True


def test_relay_wrapped_layout_json_is_extracted_and_validated() -> None:
    wrapped = "analysis omitted\n```json\n" + json.dumps(
        {
            "rotation": 0,
            "regions": [
                {"id": "q1", "type": "question_block", "box": [0.1, 0.1, 0.9, 0.8], "order": 1, "label": "1", "parent": "", "prev": False, "next": False, "minus": False, "confidence": 0.9}
            ],
        }
    ) + "\n```"
    assert parse_layout_content(wrapped)["regions"][0]["region_id"] == "q1"


def test_robust_consensus_accepts_small_box_variation_and_label_punctuation() -> None:
    proposal = _layout([0.1, 0.1, 0.5, 0.2])
    critic = _layout([0.11, 0.1, 0.51, 0.205])
    proposal["regions"][0].update({"region_type": "subquestion", "question_label": "1"})
    critic["regions"][0].update({"region_type": "subquestion", "question_label": "1."})
    result = compare_passes(proposal, critic)
    assert result["status"] == "high_confidence_silver"
    assert result["mean_matched_iou"] >= 0.82


def test_missing_content_region_requires_adjudication() -> None:
    proposal = _layout()
    critic = _layout()
    critic["regions"].append({**critic["regions"][0], "region_id": "r2", "bbox": [0.1, 0.91, 0.9, 0.99]})
    result = compare_passes(proposal, critic)
    assert result["status"] == "ambiguous"
    assert result["unmatched_critic_regions"] == 1


def test_consensus_merge_uses_union_box_and_minus_or() -> None:
    proposal = _layout([0.1, 0.1, 0.8, 0.8])
    critic = _layout([0.11, 0.09, 0.82, 0.79])
    proposal["regions"][0]["contains_critical_minus"] = False
    consensus = compare_passes(proposal, critic)
    merged = merge_consensus_layout(proposal, critic, consensus)
    assert merged["regions"][0]["bbox"] == [0.1, 0.09, 0.82, 0.8]
    assert merged["regions"][0]["contains_critical_minus"] is True


def test_unlabeled_answer_and_cross_page_types_merge_without_adjudication() -> None:
    proposal = _layout([0.1, 0.1, 0.8, 0.8])
    critic = _layout([0.11, 0.1, 0.81, 0.8])
    critic["regions"][0]["region_type"] = "cross_page_continuation"
    proposal["regions"][0]["question_label"] = ""
    critic["regions"][0]["question_label"] = ""
    consensus = compare_passes(proposal, critic)
    assert consensus["status"] == "high_confidence_silver"
    merged = merge_consensus_layout(proposal, critic, consensus)
    assert merged["regions"][0]["region_type"] == "cross_page_continuation"


def test_pending_manifest_rows_excludes_seeded_prefix_pages(workspace_tmp_path) -> None:
    rows = [
        {"page_id": f"{index:064x}", "image_sha256": f"{index + 10:064x}"}
        for index in range(3)
    ]
    output = workspace_tmp_path / "results"
    output.mkdir()
    (output / f"{rows[0]['page_id']}.json").write_text(
        json.dumps(
            {
                **rows[0],
                "final_layout": {"rotation_degrees_clockwise": 0, "regions": []},
                "teacher": {
                    "labeling_version": LABELING_VERSION,
                    "consensus_version": CONSENSUS_VERSION,
                    "quality_version": QUALITY_VERSION,
                },
            }
        ),
        encoding="utf-8",
    )

    pending = pending_manifest_rows(rows, output, max_pages=2)

    assert [row["page_id"] for row in pending] == [rows[1]["page_id"]]


def test_quality_metadata_migration_updates_only_unflagged_results(workspace_tmp_path) -> None:
    rows = [
        {"page_id": f"{index + 1:064x}", "image_sha256": f"{index + 10:064x}"}
        for index in range(2)
    ]
    output = workspace_tmp_path / "results"
    output.mkdir()
    clean = _layout([0.1, 0.1, 0.9, 0.9])
    suspicious = _layout([0.1, 0.1, 0.3, 0.13])
    suspicious["regions"][0].update({"region_type": "subquestion", "question_label": "(3)"})
    suspicious["regions"].append(
        {
            **suspicious["regions"][0],
            "region_id": "r2",
            "bbox": [0.1, 0.14, 0.9, 0.5],
            "question_label": "",
            "reading_order": 1,
        }
    )
    for row, layout in zip(rows, (clean, suspicious)):
        (output / f"{row['page_id']}.json").write_text(
            json.dumps(
                {
                    **row,
                    "teacher": {"labeling_version": LABELING_VERSION, "consensus_version": CONSENSUS_VERSION},
                    "consensus": {"status": "high_confidence_silver"},
                    "final_layout": layout,
                }
            ),
            encoding="utf-8",
        )

    report = migrate_quality_metadata(rows, output)

    migrated = json.loads((output / f"{rows[0]['page_id']}.json").read_text(encoding="utf-8"))
    untouched = json.loads((output / f"{rows[1]['page_id']}.json").read_text(encoding="utf-8"))
    assert report["migrated_pages"] == 1
    assert report["repair_required_page_ids"] == [rows[1]["page_id"]]
    assert migrated["teacher"]["quality_version"] == QUALITY_VERSION
    assert "quality_version" not in untouched["teacher"]
