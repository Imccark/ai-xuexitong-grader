from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def build_unique_manifest(
    rows: list[dict[str, Any]],
    *,
    preferred_page_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    preferred = preferred_page_ids or set()
    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        image_hash = str(row.get("image_sha256") or row.get("rectified_sha256") or "")
        if len(image_hash) != 64:
            raise ValueError(f"missing image hash for {row.get('page_id')}")
        by_hash[image_hash].append(row)

    unique_rows: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    duplicate_groups = 0
    saved_pages = 0
    for image_hash, candidates in sorted(by_hash.items()):
        candidates.sort(key=lambda row: (str(row.get("page_id")) not in preferred, str(row.get("page_id"))))
        canonical = candidates[0]
        canonical_id = str(canonical["page_id"])
        unique_rows.append(canonical)
        if len(candidates) > 1:
            duplicate_groups += 1
            saved_pages += len(candidates) - 1
        for candidate in candidates:
            aliases.append(
                {
                    "page_id": str(candidate["page_id"]),
                    "canonical_page_id": canonical_id,
                    "image_sha256": image_hash,
                    "assignment_id": str(candidate.get("assignment_id") or ""),
                    "student_hash": str(candidate.get("student_hash") or ""),
                    "is_canonical": str(candidate["page_id"]) == canonical_id,
                }
            )
    unique_rows.sort(key=lambda row: (str(row["page_id"]) not in preferred, str(row["page_id"])))
    aliases.sort(key=lambda row: str(row["page_id"]))
    report = {
        "schema_version": "1.0",
        "source_pages": len(rows),
        "unique_images": len(unique_rows),
        "duplicate_groups": duplicate_groups,
        "duplicate_alias_pages": saved_pages,
        "preferred_canonical_pages": sum(str(row["page_id"]) in preferred for row in unique_rows),
        "estimated_calls_avoided_at_2_458333_per_page": round(saved_pages * 2.458333, 3),
    }
    return unique_rows, aliases, report


def main() -> int:
    parser = argparse.ArgumentParser(description="Deduplicate exact layout images before paid teacher labeling.")
    parser.add_argument("--manifest", default="runtime_logs/teacher_labeling/all_rectified_v4_private.jsonl")
    parser.add_argument("--preferred-results", default="evaluation/layout_labels/pilot_rectified_v3_results")
    parser.add_argument("--output", default="runtime_logs/teacher_labeling/all_rectified_v4_unique_private.jsonl")
    parser.add_argument("--alias-map", default="runtime_logs/teacher_labeling/all_rectified_v4_aliases_private.jsonl")
    parser.add_argument("--report", default="datasets/layout_all_v4/deduplication_report.json")
    args = parser.parse_args()
    rows = _read_jsonl(Path(args.manifest).resolve())
    preferred_root = Path(args.preferred_results).resolve()
    preferred = {path.stem for path in preferred_root.glob("*.json") if len(path.stem) == 64}
    unique, aliases, report = build_unique_manifest(rows, preferred_page_ids=preferred)
    _write_jsonl(Path(args.output).resolve(), unique)
    _write_jsonl(Path(args.alias_map).resolve(), aliases)
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
