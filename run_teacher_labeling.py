from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from evaluation.layout_teacher import (
    QUALITY_VERSION,
    OnlineBudget,
    RelayLayoutTeacher,
    build_pilot_manifest,
    label_manifest,
    layout_quality_flags,
    read_jsonl,
    write_jsonl,
)
from prepare_rectified_labeling_images import prepare_rectified_dataset


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "configs" / "teacher_labeling.json"
DEFAULT_SOURCE_PRIVATE_MANIFEST = ROOT / "runtime_logs" / "teacher_labeling" / "pilot_private.jsonl"
DEFAULT_PRIVATE_MANIFEST = ROOT / "runtime_logs" / "teacher_labeling" / "pilot_rectified_v4_private.jsonl"
DEFAULT_PUBLIC_MANIFEST = ROOT / "evaluation" / "layout_labels" / "manifest.jsonl"
DEFAULT_OUTPUT = ROOT / "evaluation" / "layout_labels" / "pilot_rectified_v4_results"
DEFAULT_RECTIFIED_DATASET = ROOT / "datasets" / "layout_pilot_v4"
DEFAULT_FULL_PRIVATE_MANIFEST = ROOT / "runtime_logs" / "teacher_labeling" / "all_rectified_v4_unique_private.jsonl"
DEFAULT_FULL_OUTPUT = ROOT / "evaluation" / "layout_labels" / "all_rectified_v4_unique_results"


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Prepare and run budgeted Sol teacher labels for homework page layout.")
    value.add_argument("--config", default=str(DEFAULT_CONFIG))
    value.add_argument("--profile", choices=("pilot", "full"), default="pilot")
    value.add_argument("--prepare", action="store_true", help="Build deterministic private/public pilot manifests without online calls.")
    value.add_argument("--online", action="store_true", help="Explicitly allow teacher API calls.")
    value.add_argument("--manifest")
    value.add_argument("--public-manifest")
    value.add_argument("--output-dir")
    value.add_argument("--seed-from", help="Reuse current-version page results from another output directory.")
    value.add_argument("--seed-only", action="store_true", help="Validate/copy reusable results without online calls.")
    value.add_argument(
        "--quality-migrate-only",
        action="store_true",
        help="Locally mark unflagged existing results quality-current and report pages that need a repair call.",
    )
    value.add_argument("--max-pages", type=int, default=0)
    value.add_argument("--max-calls", type=int, default=0)
    value.add_argument("--max-input-tokens", type=int, default=0)
    value.add_argument("--max-output-tokens", type=int, default=0)
    value.add_argument(
        "--pass-workers",
        type=int,
        choices=(1, 2),
        default=1,
        help="Run the independent proposal and critic passes sequentially or concurrently.",
    )
    value.add_argument(
        "--page-workers",
        type=int,
        default=1,
        help="Process independent pages concurrently; use 4 with --pass-workers 2 for the tested canary profile.",
    )
    value.add_argument(
        "--request-concurrency",
        type=int,
        default=0,
        help="Global cap for in-flight API requests; defaults to page-workers * pass-workers.",
    )
    value.add_argument("--page-id", action="append", dest="page_ids", help="Label only this page id; repeat to select more pages.")
    return value


def seed_existing_results(
    manifest_rows: list[dict],
    source_dir: Path | str,
    output_dir: Path | str,
) -> dict[str, int]:
    """Copy only exact-image, current-version completed pages into a new run."""
    source_root, target_root = Path(source_dir).resolve(), Path(output_dir).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    manifest = {str(row["page_id"]): row for row in manifest_rows}
    seeded = skipped = 0
    for path in sorted(source_root.glob("*.json")):
        if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
            continue
        page_id = path.stem
        row = manifest.get(page_id)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not row or value.get("image_sha256") != row.get("image_sha256"):
            skipped += 1
            continue
        teacher = value.get("teacher") or {}
        if teacher.get("labeling_version") != config_labeling_version() or teacher.get("consensus_version") != config_consensus_version():
            skipped += 1
            continue
        if not value.get("final_layout"):
            skipped += 1
            continue
        destination = target_root / path.name
        if destination.is_file() and destination.read_bytes() == path.read_bytes():
            seeded += 1
            continue
        shutil.copy2(path, destination)
        seeded += 1
    return {"seeded_pages": seeded, "skipped_pages": skipped}


def config_labeling_version() -> str:
    from evaluation.layout_teacher import LABELING_VERSION

    return LABELING_VERSION


def config_consensus_version() -> str:
    from evaluation.layout_teacher import CONSENSUS_VERSION

    return CONSENSUS_VERSION


def migrate_quality_metadata(manifest_rows: list[dict], output_dir: Path | str) -> dict:
    root = Path(output_dir)
    migrated: list[str] = []
    already_current: list[str] = []
    repair_required: list[str] = []
    verification_required: list[str] = []
    invalid_or_missing: list[str] = []
    for row in manifest_rows:
        page_id = str(row["page_id"])
        path = root / f"{page_id}.json"
        if not path.is_file():
            invalid_or_missing.append(page_id)
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            teacher = value.get("teacher") or {}
            is_reusable = bool(
                value.get("image_sha256") == row.get("image_sha256")
                and value.get("final_layout")
                and teacher.get("labeling_version") == config_labeling_version()
                and teacher.get("consensus_version") == config_consensus_version()
            )
            if not is_reusable:
                invalid_or_missing.append(page_id)
                continue
            if teacher.get("quality_version") == QUALITY_VERSION:
                already_current.append(page_id)
                continue
            flags = layout_quality_flags(value["final_layout"])
            if flags:
                if value.get("consensus", {}).get("quality_repair_applied") and teacher.get("repair"):
                    verification_required.append(page_id)
                else:
                    repair_required.append(page_id)
                continue
            value["teacher"] = {**teacher, "quality_version": QUALITY_VERSION}
            value["consensus"] = {
                **(value.get("consensus") or {}),
                "quality_flags_before_repair": [],
                "quality_flags_after_repair": [],
                "quality_flags_after_verifier": [],
                "quality_repair_applied": False,
                "quality_verifier_applied": False,
                "quality_tiebreaker_applied": False,
                "quality_resolution": "no_persistent_candidates",
                "confirmed_retained_quality_flags": [],
                "confirmed_removed_quality_region_ids": [],
                "unresolved_quality_flags": [],
            }
            value["training_eligible"] = True
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            migrated.append(page_id)
        except (OSError, ValueError, json.JSONDecodeError):
            invalid_or_missing.append(page_id)
    report = {
        "schema_version": "1.0",
        "quality_version": QUALITY_VERSION,
        "manifest_pages": len(manifest_rows),
        "migrated_pages": len(migrated),
        "already_current_pages": len(already_current),
        "repair_required_pages": len(repair_required),
        "verification_required_pages": len(verification_required),
        "invalid_or_missing_pages": len(invalid_or_missing),
        "repair_required_page_ids": repair_required,
        "verification_required_page_ids": verification_required,
        "invalid_or_missing_page_ids": invalid_or_missing,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "quality_migration_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def pending_manifest_rows(
    manifest_rows: list[dict],
    output_dir: Path | str,
    *,
    max_pages: int,
) -> list[dict]:
    """Return prefix rows that do not already have a reusable final result."""
    root = Path(output_dir)
    pending: list[dict] = []
    for row in manifest_rows[:max_pages]:
        path = root / f"{row['page_id']}.json"
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = {}
            teacher = value.get("teacher") or {}
            if (
                value.get("image_sha256") == row.get("image_sha256")
                and value.get("final_layout")
                and teacher.get("labeling_version") == config_labeling_version()
                and teacher.get("consensus_version") == config_consensus_version()
                and (
                    teacher.get("quality_version") == QUALITY_VERSION
                    or not layout_quality_flags(value["final_layout"])
                )
            ):
                continue
        pending.append(row)
    return pending


def main() -> int:
    args = parser().parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    profile = config[args.profile]
    max_pages = args.max_pages or int(profile["max_pages"])
    manifest_path = Path(args.manifest or (DEFAULT_FULL_PRIVATE_MANIFEST if args.profile == "full" else DEFAULT_PRIVATE_MANIFEST))
    public_manifest_path = Path(args.public_manifest or DEFAULT_PUBLIC_MANIFEST)
    output_dir = Path(args.output_dir or (DEFAULT_FULL_OUTPUT if args.profile == "full" else DEFAULT_OUTPUT))
    if args.prepare:
        if args.profile != "pilot":
            raise SystemExit("full preprocessing is a separate resumable job; run prepare_all_layout_images.py")
        rows = build_pilot_manifest(ROOT, max_pages=max_pages)
        write_jsonl(DEFAULT_SOURCE_PRIVATE_MANIFEST, rows)
        prepare_rectified_dataset(
            DEFAULT_SOURCE_PRIVATE_MANIFEST,
            DEFAULT_RECTIFIED_DATASET,
            manifest_path,
            max_pages=max_pages,
        )
        public_rows = read_jsonl(DEFAULT_RECTIFIED_DATASET / "manifest.jsonl")
        write_jsonl(public_manifest_path, public_rows)
        print(json.dumps({"prepared_pages": len(public_rows), "private_manifest": str(manifest_path), "public_manifest": str(public_manifest_path), "geometry_preprocessed": True}, ensure_ascii=False))
        if not args.online:
            return 0
    manifest_rows = read_jsonl(manifest_path)
    if args.seed_from:
        seed_report = seed_existing_results(manifest_rows, args.seed_from, output_dir)
        print(json.dumps(seed_report, ensure_ascii=False))
    if args.quality_migrate_only:
        migration_report = migrate_quality_metadata(manifest_rows, output_dir)
        print(
            json.dumps(
                {
                    key: value
                    for key, value in migration_report.items()
                    if not key.endswith("_page_ids")
                }
                | {"report_path": str((output_dir / "quality_migration_report.json").resolve())},
                ensure_ascii=False,
            )
        )
        return 0
    if args.seed_only:
        return 0
    if not args.online:
        raise SystemExit("online labeling is disabled; pass --online with explicit budgets")
    if min(args.max_calls, args.max_input_tokens, args.max_output_tokens) <= 0:
        raise SystemExit("--online requires positive call, input-token, and output-token budgets")
    if args.page_ids:
        selected_ids = set(args.page_ids)
        manifest_rows = [row for row in manifest_rows if str(row.get("page_id")) in selected_ids]
        missing_ids = selected_ids - {str(row.get("page_id")) for row in manifest_rows}
        if missing_ids:
            raise SystemExit(f"unknown --page-id values: {len(missing_ids)}")
        max_pages = len(manifest_rows)
    pending_rows = pending_manifest_rows(manifest_rows, output_dir, max_pages=max_pages)
    if args.max_calls < 2 * len(pending_rows):
        raise SystemExit(
            "--max-calls must allow two independent passes per pending page; "
            f"pending={len(pending_rows)}, minimum_calls={2 * len(pending_rows)}"
        )
    prefix_pages = min(max_pages, len(manifest_rows))
    print(
        json.dumps(
            {
                "prefix_pages": prefix_pages,
                "reused_pages": prefix_pages - len(pending_rows),
                "pending_pages": len(pending_rows),
            },
            ensure_ascii=False,
        )
    )
    budget = OnlineBudget(args.max_calls, args.max_input_tokens, args.max_output_tokens)
    if args.page_workers <= 0:
        raise SystemExit("--page-workers must be positive")
    request_concurrency = args.request_concurrency or args.page_workers * args.pass_workers
    if request_concurrency <= 0:
        raise SystemExit("--request-concurrency must be positive")
    teacher = RelayLayoutTeacher.from_config(
        config,
        budget=budget,
        max_request_concurrency=request_concurrency,
    )
    report = label_manifest(
        manifest_rows,
        teacher,
        output_dir=output_dir,
        max_pages=max_pages,
        pass_workers=args.pass_workers,
        page_workers=args.page_workers,
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["failed_pages"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
