from __future__ import annotations

import json

from tools.evaluation.core.compute_metrics import main


def test_compute_metrics_cli_accepts_model_only_evaluation(workspace_tmp_path, monkeypatch, capsys) -> None:
    output = workspace_tmp_path / "metrics.json"
    monkeypatch.setattr(
        "sys.argv",
        ["compute_metrics", "--candidate-root", str(workspace_tmp_path), "--output", str(output)],
    )
    assert main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["gold_path"] is None
    assert report["teacher_gold_confirmed"] is False
    assert report["question_verdict_accuracy"]["value"] is None
    assert "gold_records" in capsys.readouterr().out
