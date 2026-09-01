from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.layout_teacher import build_pilot_manifest, write_jsonl
from prepare_rectified_labeling_images import prepare_rectified_dataset


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_MANIFEST = ROOT / "runtime_logs" / "teacher_labeling" / "all_pages_private.jsonl"
DEFAULT_OUTPUT = ROOT / "datasets" / "layout_all_v4"
DEFAULT_PRIVATE_OUTPUT = ROOT / "runtime_logs" / "teacher_labeling" / "all_rectified_v4_private.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description="Geometry- and orientation-normalize every homework page in the repository.")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--source-manifest", default=str(DEFAULT_SOURCE_MANIFEST))
    parser.add_argument("--private-output", default=str(DEFAULT_PRIVATE_OUTPUT))
    parser.add_argument("--orientation-overrides")
    args = parser.parse_args()
    if args.workers <= 0:
        raise SystemExit("--workers must be positive")

    rows = build_pilot_manifest(ROOT, max_pages=10_000_000)
    write_jsonl(Path(args.source_manifest), rows)
    override_rows = []
    if args.orientation_overrides:
        override_rows = [
            json.loads(line)
            for line in Path(args.orientation_overrides).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    orientation_overrides = {str(row.get("page_id")): row for row in override_rows}
    if len(orientation_overrides) != len(override_rows):
        raise SystemExit("orientation override file contains duplicate page_id values")
    report = prepare_rectified_dataset(
        args.source_manifest,
        args.output_dir,
        args.private_output,
        max_pages=0,
        workers=args.workers,
        orientation_overrides=orientation_overrides,
    )
    print(json.dumps({"discovered_pages": len(rows), **report}, ensure_ascii=False))
    return 0 if report["failed_pages"] == 0 and report["completed_pages"] == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
