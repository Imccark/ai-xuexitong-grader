import argparse
import json
import re
from pathlib import Path

from project_config import (
    DEFAULT_ANSWER_KEY_FILENAME,
    REPO_ROOT,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="一键创建新一周目录和 assignment 配置。")
    parser.add_argument("week_name", help="新周目录名称，例如：第二周")
    parser.add_argument(
        "--force",
        action="store_true",
        help="若周目录或 assignment 配置已存在，则允许覆盖配置文件并复用目录",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只预览将要创建的目录和配置，不实际写入",
    )
    return parser.parse_args()


def sanitize_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name or "new_week"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_assignment_payload(
    new_week_name: str,
    new_week_dir: Path,
    new_answer_key_name: str,
) -> tuple[Path, dict]:
    config_dir = REPO_ROOT / "configs" / "assignments"
    safe_week_name = sanitize_name(new_week_name)
    new_assignment_id = safe_week_name
    new_assignment_filename = f"{new_assignment_id}.json"
    new_assignment_path = config_dir / new_assignment_filename

    payload = {
        "assignment_id": new_assignment_id,
        "week_name": new_week_name,
        "week_dir": str(Path("../../") / new_week_dir.name),
        "answer_key": str(Path("../../") / new_week_dir.name / new_answer_key_name),
        "raw_submissions_dir": str(Path("../../") / new_week_dir.name / "raw_submissions"),
        "processed_images_dir": str(Path("../../") / new_week_dir.name / "processed_images"),
        "results_dir": str(Path("../../") / new_week_dir.name / "results"),
        "preprocess_summary": str(Path("../../") / new_week_dir.name / "preprocess_summary.txt"),
        "grading_summary": str(Path("../../") / new_week_dir.name / "summary.txt"),
    }
    return new_assignment_path, payload


def main() -> int:
    args = parse_args()
    week_name = args.week_name.strip()
    if not week_name:
        raise SystemExit("week_name 不能为空。")

    new_week_dir = REPO_ROOT / week_name
    if new_week_dir.exists() and not args.force:
        raise SystemExit(f"周目录已存在：{new_week_dir}。如需继续，请加 --force")

    answer_key_name = DEFAULT_ANSWER_KEY_FILENAME
    new_assignment_path, assignment_payload = build_assignment_payload(
        new_week_name=week_name,
        new_week_dir=new_week_dir,
        new_answer_key_name=answer_key_name,
    )

    if new_assignment_path.exists() and not args.force:
        raise SystemExit(f"assignment 配置已存在：{new_assignment_path}。如需继续，请加 --force")

    directories = [
        new_week_dir,
        new_week_dir / "raw_submissions",
        new_week_dir / "processed_images",
        new_week_dir / "results",
        new_week_dir / "temp_workspace",
    ]

    answer_key_path = new_week_dir / answer_key_name
    summary_paths = [new_week_dir / "preprocess_summary.txt", new_week_dir / "summary.txt"]

    if args.dry_run:
        print(f"[DRY-RUN] 将创建周目录：{new_week_dir}")
        print(f"[DRY-RUN] 将创建 assignment：{new_assignment_path}")
        print(f"[DRY-RUN] 将创建目录：{', '.join(str(path) for path in directories)}")
        print(f"[DRY-RUN] 将创建标准答案文件：{answer_key_path}")
        return 0

    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    if not answer_key_path.exists():
        answer_key_path.write_text("% 在这里填写本周标准答案\n", encoding="utf-8")

    for summary_path in summary_paths:
        if not summary_path.exists():
            summary_path.write_text("", encoding="utf-8")

    write_json(new_assignment_path, assignment_payload)

    print(f"[OK] 已创建周目录：{new_week_dir}")
    print(f"[OK] 已创建 assignment：{new_assignment_path}")
    print(f"[NEXT] 填写标准答案：{answer_key_path}")
    print(f"[NEXT] 前处理命令：python run_preprocessing.py --assignment {new_assignment_path.relative_to(REPO_ROOT)}")
    print(f"[NEXT] 批量评分命令：python run_batch_grading.py --assignment {new_assignment_path.relative_to(REPO_ROOT)} --max-workers 4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
