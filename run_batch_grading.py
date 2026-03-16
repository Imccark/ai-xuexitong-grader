import argparse
import concurrent.futures
import re
from dataclasses import dataclass
from pathlib import Path

from grade_evaluator import (
    evaluate_homework_qwen_vision,
    get_student_result_path,
    sanitize_filename,
    write_failure_result,
)
from project_config import load_runtime_config


PAGE_PATTERN = re.compile(r"page_(\d+)\.png$", re.IGNORECASE)


@dataclass
class StudentTaskResult:
    student_id: str
    status: str
    attempts: int
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量批改已整理好的学生作业图片。")
    parser.add_argument("--week", help="周目录，例如：第一周；如果仓库里只有一个 assignment，可省略")
    parser.add_argument("--assignment", help="assignment 配置文件路径；如果仓库里只有一个 assignment，可省略")
    parser.add_argument("--max-workers", type=int, default=4, help="最大并发数，默认 4")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="若已有结果文件属于失败占位结果，则允许重新评分",
    )
    parser.add_argument(
        "--answer-key",
        "--tex",
        dest="answer_key",
        default=None,
        help="手动指定标准答案路径；默认使用 assignment 配置中的 answer_key",
    )
    return parser.parse_args()


def get_page_images(student_dir: Path) -> list[str]:
    matched_files: list[tuple[int, Path]] = []
    for path in student_dir.iterdir():
        if not path.is_file():
            continue
        match = PAGE_PATTERN.match(path.name)
        if match:
            matched_files.append((int(match.group(1)), path))
    matched_files.sort(key=lambda item: item[0])
    return [str(path) for _, path in matched_files]


def has_non_empty_file(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def is_failed_placeholder_result(path: Path) -> bool:
    if not has_non_empty_file(path):
        return False
    content = path.read_text(encoding="utf-8", errors="ignore")
    return "需人工复核" in content


def grade_one_student(
    student_dir: Path,
    tex_path: str,
    results_dir: Path,
    retry_failed: bool,
    subject_config,
) -> StudentTaskResult:
    student_id = sanitize_filename(student_dir.name)
    result_path = Path(get_student_result_path(student_id, str(results_dir)))

    if has_non_empty_file(result_path):
        if retry_failed and is_failed_placeholder_result(result_path):
            print(f"[RETRY-FAILED] {student_id}")
        else:
            print(f"[SKIP] {student_id}")
            return StudentTaskResult(student_id=student_id, status="skipped", attempts=0)

    image_paths = get_page_images(student_dir)
    if not image_paths:
        error = "未找到符合 page_*.png 的标准化图片"
        write_failure_result(
            student_name=student_id,
            output_dir=str(results_dir),
            overall="图片缺失，需人工复核",
            error_lines=[error],
            advice_lines=["请先完成前处理，确保该学生目录下存在 page_1.png 等标准化图片。"],
        )
        print(f"[FAIL] {student_id} | {error}")
        return StudentTaskResult(student_id=student_id, status="failed", attempts=1, error=error)

    max_attempts = 2
    last_error = "评分失败"

    for attempt in range(1, max_attempts + 1):
        success = evaluate_homework_qwen_vision(
            tex_path=tex_path,
            student_name=student_id,
            image_paths=image_paths,
            output_dir=str(results_dir),
            subject_config=subject_config,
            show_reasoning=False,
            show_final_result=False,
            reasoning_log_path=None,
        )
        if success and has_non_empty_file(result_path):
            print(f"[OK] {student_id}")
            return StudentTaskResult(student_id=student_id, status="success", attempts=attempt)
        last_error = f"评分脚本返回失败，第 {attempt} 次尝试未成功"
        if attempt < max_attempts:
            print(f"[RETRY] {student_id} | 第 {attempt + 1} 次尝试")

    print(f"[FAIL] {student_id} | {last_error}")
    return StudentTaskResult(student_id=student_id, status="failed", attempts=max_attempts, error=last_error)


def write_summary(summary_path: Path, week_name: str, task_results: list[StudentTaskResult]) -> Path:
    total = len(task_results)
    success_count = sum(1 for item in task_results if item.status == "success")
    failed_items = [item for item in task_results if item.status == "failed"]
    failed_count = len(failed_items)
    skipped_count = sum(1 for item in task_results if item.status == "skipped")

    lines = [
        f"周目录：{week_name}",
        f"总学生数：{total}",
        f"成功数：{success_count}",
        f"失败数：{failed_count}",
        f"跳过数：{skipped_count}",
        "失败学生列表：",
    ]

    if failed_items:
        for item in failed_items:
            if item.error:
                lines.append(f"- {item.student_id}: {item.error}")
            else:
                lines.append(f"- {item.student_id}")
    else:
        lines.append("- 无")

    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path


def main() -> int:
    args = parse_args()
    try:
        runtime_config = load_runtime_config(assignment=args.assignment, week=args.week)
    except ValueError as exc:
        raise SystemExit(str(exc))
    week_dir = runtime_config.week_dir
    tex_path = Path(args.answer_key).resolve() if args.answer_key else runtime_config.answer_key_path
    processed_dir = runtime_config.processed_images_dir
    results_dir = runtime_config.results_dir
    summary_path = runtime_config.grading_summary_path

    if not week_dir.is_dir():
        raise SystemExit(f"周目录不存在：{week_dir}")
    if not tex_path.is_file():
        raise SystemExit(f"标准答案文件不存在：{tex_path}")
    if not processed_dir.is_dir():
        raise SystemExit(f"标准化图片目录不存在：{processed_dir}")
    if args.max_workers < 1:
        raise SystemExit("--max-workers 必须大于等于 1")

    print(f"[CONFIG] {runtime_config.assignment_id} | {runtime_config.subject.subject_name} | {runtime_config.week_name}")
    results_dir.mkdir(parents=True, exist_ok=True)
    student_dirs = sorted(path for path in processed_dir.iterdir() if path.is_dir())

    if not student_dirs:
        summary_path = write_summary(summary_path, runtime_config.week_name, [])
        print(f"[DONE] 未发现学生目录，summary 已写入 {summary_path}")
        return 0

    task_results: list[StudentTaskResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        future_map = {
            executor.submit(
                grade_one_student,
                student_dir,
                str(tex_path),
                results_dir,
                args.retry_failed,
                runtime_config.subject,
            ): student_dir
            for student_dir in student_dirs
        }
        for future in concurrent.futures.as_completed(future_map):
            student_dir = future_map[future]
            student_id = sanitize_filename(student_dir.name)
            try:
                task_results.append(future.result())
            except Exception as exc:
                error = f"批量任务异常：{exc}"
                write_failure_result(
                    student_name=student_id,
                    output_dir=str(results_dir),
                    overall="判卷脚本调用失败，需人工复核",
                    error_lines=[error],
                    advice_lines=["请检查该学生图片目录和接口状态后重试。"],
                )
                print(f"[FAIL] {student_id} | {error}")
                task_results.append(
                    StudentTaskResult(
                        student_id=student_id,
                        status="failed",
                        attempts=1,
                        error=error,
                    )
                )

    task_results.sort(key=lambda item: item.student_id)
    summary_path = write_summary(summary_path, runtime_config.week_name, task_results)
    print(f"[DONE] summary 已写入 {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
