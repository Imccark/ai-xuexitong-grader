from __future__ import annotations

import json

from evaluation.audit_layout_labels import audit_layout_results
from evaluation.layout_teacher import CONSENSUS_VERSION, LABELING_VERSION, QUALITY_VERSION


def test_layout_audit_passes_current_anonymous_result(workspace_tmp_path) -> None:
    page_id = "a" * 64
    manifest = workspace_tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps({"page_id": page_id}) + "\n", encoding="utf-8")
    results = workspace_tmp_path / "results"
    results.mkdir()
    meta = {
        "reported_model": "gpt-5.6-sol",
        "prompt_tokens": 100,
        "completion_tokens": 200,
    }
    result = {
        "page_id": page_id,
        "teacher": {
            "labeling_version": LABELING_VERSION,
            "consensus_version": CONSENSUS_VERSION,
            "quality_version": QUALITY_VERSION,
            "proposal": meta,
            "critic": meta,
            "adjudicator": None,
        },
        "consensus": {
            "status": "high_confidence_silver",
            "quality_repair_applied": False,
            "quality_flags_after_repair": [],
        },
        "final_layout": {
            "rotation_degrees_clockwise": 0,
            "regions": [
                {
                    "region_id": "q1",
                    "region_type": "question_block",
                    "bbox": [0.1, 0.1, 0.9, 0.9],
                    "reading_order": 1,
                    "question_label": "1",
                    "parent_region_id": "",
                    "continues_from_previous_page": False,
                    "continues_to_next_page": False,
                    "contains_critical_minus": True,
                    "confidence": 0.95,
                }
            ],
        },
    }
    (results / f"{page_id}.json").write_text(json.dumps(result), encoding="utf-8")

    report = audit_layout_results(manifest, results)

    assert report["automatic_gate_passed"] is True
    assert report["average_calls_per_page"] == 2.0
    assert report["secret_hits"] == 0
