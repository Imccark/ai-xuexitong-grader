from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tools.evaluation.core.layout_teacher import (
    CONSENSUS_VERSION,
    QUALITY_VERSION,
    compare_passes,
    merge_consensus_layout,
    write_jsonl,
)


def reconcile_result(result: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    consensus = compare_passes(result["proposal"], result["critic"])
    previous_consensus = result.get("consensus") or {}
    quality_fields = {
        key: previous_consensus[key]
        for key in (
            "quality_flags_before_repair",
            "quality_flags_after_repair",
            "quality_repair_applied",
        )
        if key in previous_consensus
    }
    resolved_without_adjudicator = consensus["status"] == "high_confidence_silver"
    if resolved_without_adjudicator:
        final_layout = merge_consensus_layout(result["proposal"], result["critic"], consensus)
        adjudicator = None
        adjudicator_meta = None
    else:
        adjudicator = result.get("adjudicator")
        adjudicator_meta = result.get("teacher", {}).get("adjudicator")
        if adjudicator:
            consensus = {
                **consensus,
                "status": "adjudicated_silver",
                "adjudicator_region_count": len(adjudicator.get("regions") or []),
            }
            final_layout = adjudicator
        else:
            final_layout = result.get("final_layout")
    if quality_fields.get("quality_repair_applied") and result.get("repair"):
        final_layout = result["repair"]
    consensus = {**consensus, **quality_fields}
    teacher = {
        **result.get("teacher", {}),
        "consensus_version": CONSENSUS_VERSION,
        "quality_version": QUALITY_VERSION,
        "adjudicator": adjudicator_meta,
    }
    return (
        {
            **result,
            "teacher": teacher,
            "adjudicator": adjudicator,
            "repair": result.get("repair"),
            "consensus": consensus,
            "final_layout": final_layout,
        },
        resolved_without_adjudicator,
    )


def reconcile_directory(source_dir: Path | str, output_dir: Path | str) -> dict[str, Any]:
    source = Path(source_dir)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    newly_resolved: list[str] = []
    for path in sorted(source.glob("*.json")):
        if len(path.stem) != 64:
            continue
        original = json.loads(path.read_text(encoding="utf-8"))
        reconciled, resolved_without_adjudicator = reconcile_result(original)
        if original.get("consensus", {}).get("status") == "adjudicated_silver" and resolved_without_adjudicator:
            newly_resolved.append(str(original["page_id"]))
        (output / path.name).write_text(
            json.dumps(reconciled, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        rows.append(reconciled)
    write_jsonl(output / "labels.jsonl", rows)
    report = {
        "schema_version": "1.0",
        "consensus_version": CONSENSUS_VERSION,
        "pages": len(rows),
        "high_confidence_silver": sum(row["consensus"]["status"] == "high_confidence_silver" for row in rows),
        "adjudicated_silver": sum(row["consensus"]["status"] == "adjudicated_silver" for row in rows),
        "newly_resolved_page_ids": newly_resolved,
    }
    (output / "reconciliation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay layout consensus without provider calls.")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    report = reconcile_directory(args.source_dir, args.output_dir)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
