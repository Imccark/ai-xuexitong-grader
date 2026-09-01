from __future__ import annotations

import json

from tools.layout.derive_layout_orientation_overrides import derive_orientation_overrides
from tools.evaluation.core.layout_teacher import CONSENSUS_VERSION, LABELING_VERSION


def _result(page_id: str, image_sha: str, rotations: tuple[int, int, int]) -> dict:
    proposal, critic, adjudicator = rotations
    meta = {"reported_model": "gpt-5.6-sol"}
    return {
        "page_id": page_id,
        "image_sha256": image_sha,
        "proposal": {"rotation_degrees_clockwise": proposal},
        "critic": {"rotation_degrees_clockwise": critic},
        "adjudicator": {"rotation_degrees_clockwise": adjudicator},
        "final_layout": {"rotation_degrees_clockwise": adjudicator},
        "teacher": {
            "labeling_version": LABELING_VERSION,
            "consensus_version": CONSENSUS_VERSION,
            "proposal": meta,
            "critic": meta,
            "adjudicator": meta,
        },
    }


def test_strict_consensus_orientation_override_is_accepted(workspace_tmp_path) -> None:
    page_id, image_sha = "a" * 64, "b" * 64
    results = workspace_tmp_path / "results"
    results.mkdir()
    (results / f"{page_id}.json").write_text(json.dumps(_result(page_id, image_sha, (90, 90, 90))), encoding="utf-8")

    overrides, report = derive_orientation_overrides(
        [{"page_id": page_id, "image_sha256": image_sha}],
        results,
        expected_model="gpt-5.6-sol",
    )

    assert len(overrides) == 1
    assert overrides[0]["rotation_degrees_clockwise"] == 90
    assert overrides[0]["votes"] == {"proposal": 90, "critic": 90, "adjudicator": 90}
    assert report["automatic_gate_passed"] is True


def test_disagreeing_orientation_votes_are_not_auto_applied(workspace_tmp_path) -> None:
    page_id, image_sha = "c" * 64, "d" * 64
    results = workspace_tmp_path / "results"
    results.mkdir()
    (results / f"{page_id}.json").write_text(json.dumps(_result(page_id, image_sha, (90, 0, 90))), encoding="utf-8")

    overrides, report = derive_orientation_overrides(
        [{"page_id": page_id, "image_sha256": image_sha}],
        results,
        expected_model="gpt-5.6-sol",
    )

    assert overrides == []
    assert report["automatic_gate_passed"] is False
    assert report["unresolved"][0]["reason"] == "independent_rotation_votes_disagree"
