import argparse
import concurrent.futures
import json
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
ITEM_START_PATTERN = re.compile(r"^\s*(?:[-*]\s*)?(\d+)[\.\、\)]\s*(.*)$")
SECTION_LINE_PATTERN = re.compile(r"^\s*([^：:]+?)\s*[：:]\s*(.*)$")
QUESTION_PATTERNS = (
    re.compile(r"第\s*([0-9]+(?:\.[0-9]+)+(?:\s*\([0-9]+\))?)\s*题"),
    re.compile(r"([0-9]+(?:\.[0-9]+)+(?:\s*\([0-9]+\))?)"),
)


@dataclass
class StudentTaskResult:
    student_id: str
    status: str
    attempts: int
    error: str | None = None


def strip_trailing_colon(text: str) -> str:
    return text.strip().rstrip("：:").strip()


def parse_format_config(output_format: str) -> tuple[str | None, str | None, list[str]]:
    name_label: str | None = None
    overall_label: str | None = None
    section_labels: list[str] = []
    for raw_line in output_format.splitlines():
        line = raw_line.strip()
        if not line or set(line) == {"="}:
            continue
        if "{student_name}" in line:
            name_label = strip_trailing_colon(line.split("{student_name}", 1)[0])
            continue
        if line.startswith("整体情况"):
            overall_label = strip_trailing_colon(line.split("[", 1)[0])
            continue
        if line.endswith("：") or line.endswith(":"):
            label = strip_trailing_colon(line)
            if label not in section_labels:
                section_labels.append(label)
    return name_label, overall_label, section_labels


def split_numbered_items(lines: list[str]) -> list[str]:
    items: list[str] = []
    current_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            if current_lines:
                current_lines.append("")
            continue
        item_match = ITEM_START_PATTERN.match(line)
        if item_match:
            if current_lines:
                items.append("\n".join(current_lines).strip())
            content = item_match.group(2).strip()
            current_lines = [content] if content else []
            continue
        if current_lines:
            current_lines.append(line.strip())
        else:
            current_lines = [line.strip()]
    if current_lines:
        items.append("\n".join(current_lines).strip())
    return [item for item in items if item]


def extract_question_ids(text: str) -> list[str]:
    questions: list[str] = []
    for pattern in QUESTION_PATTERNS:
        for match in pattern.findall(text):
            question_id = " ".join(match.split())
            if question_id not in questions:
                questions.append(question_id)
    return questions


def build_details_by_question(items: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for item in items:
        question_ids = extract_question_ids(item)
        if not question_ids:
            grouped.setdefault("未识别题号", []).append(item)
            continue
        for question_id in question_ids:
            grouped.setdefault(question_id, []).append(item)
    return grouped


def parse_result_text(result_text: str, output_format: str) -> dict:
    name_label, overall_label, section_labels = parse_format_config(output_format)
    section_order = [*section_labels]
    known_labels = [label for label in [name_label, overall_label, *section_order] if label]
    lines = [line.rstrip("\n") for line in result_text.replace("\r\n", "\n").split("\n")]

    student_name = ""
    overall = ""
    section_lines: dict[str, list[str]] = {label: [] for label in section_order}
    current_section: str | None = None

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or set(stripped) == {"="}:
            continue
        match = SECTION_LINE_PATTERN.match(stripped)
        if match:
            label = strip_trailing_colon(match.group(1))
            value = match.group(2).strip()
            if name_label and label == name_label:
                student_name = value
                current_section = None
                continue
            if overall_label and label == overall_label:
                overall = value
                current_section = None
                continue
            if label in section_lines:
                current_section = label
                if value:
                    section_lines[label].append(value)
                continue
            if label in known_labels:
                current_section = None
                continue
        if current_section:
            section_lines[current_section].append(raw_line)

    parsed_sections: dict[str, dict] = {}
    for label, raw_items in section_lines.items():
        items = split_numbered_items(raw_items)
        parsed_sections[label] = {
            "raw_text": "\n".join(raw_items).strip(),
            "items": items,
        }

    error_section_label = next((label for label in section_order if "错误细节" in label), None)
    proof_section_label = next((label for label in section_order if "证明题审查" in label), None)
    error_items = parsed_sections.get(error_section_label, {}).get("items", []) if error_section_label else []
    proof_items = parsed_sections.get(proof_section_label, {}).get("items", []) if proof_section_label else []
    errors_by_question = build_details_by_question(error_items)
    proofs_by_question = build_details_by_question(proof_items)

    return {
        "student_name_or_id": student_name,
        "overall": overall,
        "modules": parsed_sections,
        "error_details_by_question": errors_by_question,
        "proof_review_by_question": proofs_by_question,
    }


def write_result_json(result_path: Path, subject_config) -> Path:
    result_text = result_path.read_text(encoding="utf-8", errors="ignore")
    parsed = parse_result_text(result_text, subject_config.output_format)
    payload = {
        "result_txt": str(result_path),
        "parsed_with_output_format": True,
        **parsed,
    }
    json_path = result_path.with_suffix(".json")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return json_path


def generate_structured_results(student_dirs: list[Path], results_dir: Path, subject_config) -> tuple[int, int]:
    success_count = 0
    failed_count = 0
    for student_dir in student_dirs:
        student_id = sanitize_filename(student_dir.name)
        result_path = Path(get_student_result_path(student_id, str(results_dir)))
        if not has_non_empty_file(result_path):
            failed_count += 1
            print(f"[WARN] 缺少结果 txt，跳过 JSON 生成: {student_id}")
            continue
        try:
            json_path = write_result_json(result_path, subject_config)
            success_count += 1
            print(f"[JSON] {student_id} -> {json_path.name}")
        except Exception as exc:
            failed_count += 1
            print(f"[WARN] JSON 生成失败: {student_id} | {exc}")
    return success_count, failed_count


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
    json_success, json_failed = generate_structured_results(student_dirs, results_dir, runtime_config.subject)
    print(f"[DONE] JSON 生成完成：成功 {json_success}，失败 {json_failed}")
    summary_path = write_summary(summary_path, runtime_config.week_name, task_results)
    print(f"[DONE] summary 已写入 {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
