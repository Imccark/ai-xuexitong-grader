import argparse
import concurrent.futures
import io
import os
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from grade_evaluator import sanitize_filename
from grading_graph.nodes.ingest import IngestLimits, _safe_member_name
from grading_graph.nodes.image_quality import normalize_image_bytes
from pdf_helper import pdf_to_images
from project_config import load_runtime_config


SUPPORTED_IMAGE_FORMATS = {".jpg", ".jpeg", ".png"}


@dataclass
class StudentPreprocessResult:
    student_id: str
    status: str
    page_count: int
    detail: str | None = None


@dataclass
class CandidateFile:
    relative_name: str
    kind: str
    data: bytes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量前处理学生原始提交，生成标准化图片目录。")
    parser.add_argument("--week", help="周目录，例如：第一周；如果仓库里只有一个 assignment，可省略")
    parser.add_argument("--assignment", help="assignment 配置文件路径；如果仓库里只有一个 assignment，可省略")
    parser.add_argument("--max-workers", type=int, default=4, help="最大并发数，默认 4")
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="即使已有 page_1.png，也重新生成该学生的标准化图片",
    )
    return parser.parse_args()


def natural_sort_key(name: str) -> list:
    parts: list = []
    current = ""
    current_is_digit = None
    for char in name:
        is_digit = char.isdigit()
        if current_is_digit is None or is_digit == current_is_digit:
            current += char
        else:
            parts.append(int(current) if current_is_digit else current.lower())
            current = char
        current_is_digit = is_digit
    if current:
        parts.append(int(current) if current_is_digit else current.lower())
    return parts


def parse_student_id(raw_path: Path) -> str:
    stem = raw_path.stem
    return sanitize_filename(stem)


def has_processed_pages(student_dir: Path) -> bool:
    return student_dir.is_dir() and any(student_dir.glob("page_*.png"))


def detect_kind(file_name: str, data: bytes) -> str | None:
    if zipfile.is_zipfile(io.BytesIO(data)):
        return "zip"
    if data.startswith(b"%PDF"):
        return "pdf"
    suffix = Path(file_name).suffix.lower()
    if suffix in SUPPORTED_IMAGE_FORMATS:
        return "image"
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        return "image"
    except Exception:
        return None


def collect_candidate_files(
    data: bytes,
    file_name: str,
    prefix: str = "",
    *,
    limits: IngestLimits | None = None,
    depth: int = 0,
    counters: dict[str, int] | None = None,
) -> list[CandidateFile]:
    limits = limits or IngestLimits()
    counters = counters or {"files": 0, "bytes": 0}
    safe_file_name = _safe_member_name(file_name)
    if len(data) > limits.max_member_bytes:
        raise ValueError(f"member exceeds size limit: {safe_file_name}")
    kind = detect_kind(file_name, data)
    relative_name = f"{prefix}{safe_file_name}"

    if kind == "zip":
        if depth >= limits.max_zip_depth:
            raise ValueError("nested archive depth exceeds limit")
        collected: list[CandidateFile] = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                member_name = _safe_member_name(member.filename)
                if member.file_size > limits.max_member_bytes:
                    raise ValueError(f"member exceeds size limit: {member_name}")
                counters["files"] += 1
                counters["bytes"] += member.file_size
                if counters["files"] > limits.max_files:
                    raise ValueError("file count exceeds limit")
                if counters["bytes"] > limits.max_uncompressed_bytes:
                    raise ValueError("uncompressed archive size exceeds limit")
                collected.extend(
                    collect_candidate_files(
                        archive.read(member),
                        member_name,
                        prefix=f"{relative_name}/",
                        limits=limits,
                        depth=depth + 1,
                        counters=counters,
                    )
                )
        return collected

    if kind in {"pdf", "image"}:
        return [CandidateFile(relative_name=relative_name, kind=kind, data=data)]

    return []


def convert_image_bytes_to_png(image_bytes: bytes, output_path: Path) -> None:
    normalized_bytes, _metadata = normalize_image_bytes(image_bytes)
    output_path.write_bytes(normalized_bytes)


def clear_processed_pages(student_output_dir: Path) -> None:
    if not student_output_dir.exists():
        return
    for path in student_output_dir.glob("page_*.png"):
        path.unlink()


def commit_staged_pages(staged_dir: Path, target_dir: Path, backup_root: Path) -> Path | None:
    """Atomically publish validated pages while retaining the previous directory."""
    backup_dir: Path | None = None
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir(parents=True, exist_ok=True)
    if target_dir.exists():
        backup_dir = backup_root / f"{target_dir.name}-{uuid.uuid4().hex}"
        os.replace(target_dir, backup_dir)
    try:
        os.replace(staged_dir, target_dir)
    except Exception:
        if backup_dir is not None and not target_dir.exists():
            os.replace(backup_dir, target_dir)
        raise
    return backup_dir


def build_candidates_from_raw(raw_zip_path: Path, *, limits: IngestLimits | None = None) -> list[CandidateFile]:
    collected = collect_candidate_files(
        raw_zip_path.read_bytes(),
        raw_zip_path.name,
        limits=limits,
    )
    collected.sort(key=lambda item: natural_sort_key(item.relative_name))
    return collected


def preprocess_one_student(
    raw_zip_path: Path,
    processed_dir: Path,
    temp_root: Path,
    reprocess: bool,
    backup_root: Path | None = None,
) -> StudentPreprocessResult:
    student_id = parse_student_id(raw_zip_path)
    student_output_dir = processed_dir / student_id

    if has_processed_pages(student_output_dir) and not reprocess:
        print(f"[SKIP] {student_id}")
        page_count = len(list(student_output_dir.glob("page_*.png")))
        return StudentPreprocessResult(student_id=student_id, status="skipped", page_count=page_count)

    candidates = build_candidates_from_raw(raw_zip_path)
    if not candidates:
        print(f"[FAIL] {student_id} | 未识别到 PDF 或图片")
        return StudentPreprocessResult(
            student_id=student_id,
            status="failed",
            page_count=0,
            detail="未识别到可处理的 PDF 或图片文件",
        )

    student_temp_dir = temp_root / student_id
    if student_temp_dir.exists():
        shutil.rmtree(student_temp_dir)
    student_temp_dir.mkdir(parents=True, exist_ok=True)
    staged_pages_dir = student_temp_dir / "pages"
    staged_pages_dir.mkdir(parents=True, exist_ok=True)

    page_index = 1
    try:
        for candidate in candidates:
            if candidate.kind == "pdf":
                temp_pdf_path = student_temp_dir / Path(candidate.relative_name).name
                temp_pdf_path.write_bytes(candidate.data)
                pdf_output_dir = student_temp_dir / f"pdf_pages_{page_index}"
                generated_paths = pdf_to_images(str(temp_pdf_path), str(pdf_output_dir))
                if not generated_paths:
                    raise RuntimeError(f"PDF 转图失败：{candidate.relative_name}")
                for generated_path in sorted(generated_paths, key=natural_sort_key):
                    target_path = staged_pages_dir / f"page_{page_index}.png"
                    convert_image_bytes_to_png(Path(generated_path).read_bytes(), target_path)
                    page_index += 1
            elif candidate.kind == "image":
                target_path = staged_pages_dir / f"page_{page_index}.png"
                convert_image_bytes_to_png(candidate.data, target_path)
                page_index += 1

        page_count = page_index - 1
        if page_count == 0:
            raise RuntimeError("未生成任何标准化图片")

        for page_path in staged_pages_dir.glob("page_*.png"):
            with Image.open(page_path) as image:
                image.verify()
        commit_staged_pages(
            staged_pages_dir,
            student_output_dir,
            backup_root or (temp_root.parent / "preprocess_backups"),
        )

        print(f"[OK] {student_id} | {page_count} 页")
        return StudentPreprocessResult(student_id=student_id, status="success", page_count=page_count)
    except Exception as exc:
        print(f"[FAIL] {student_id} | {exc}")
        return StudentPreprocessResult(
            student_id=student_id,
            status="failed",
            page_count=0,
            detail=str(exc),
        )
    finally:
        shutil.rmtree(student_temp_dir, ignore_errors=True)


def write_summary(summary_path: Path, week_name: str, results: list[StudentPreprocessResult]) -> Path:
    total = len(results)
    success_count = sum(1 for item in results if item.status == "success")
    failed_items = [item for item in results if item.status == "failed"]
    skipped_count = sum(1 for item in results if item.status == "skipped")

    lines = [
        f"周目录：{week_name}",
        f"总学生数：{total}",
        f"成功数：{success_count}",
        f"失败数：{len(failed_items)}",
        f"跳过数：{skipped_count}",
        "失败学生列表：",
    ]
    if failed_items:
        for item in failed_items:
            lines.append(f"- {item.student_id}: {item.detail or '处理失败'}")
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
    raw_dir = runtime_config.raw_submissions_dir
    processed_dir = runtime_config.processed_images_dir
    temp_root = week_dir / "temp_workspace" / "preprocess"
    backup_root = week_dir / "temp_workspace" / "preprocess_backups"
    summary_path = runtime_config.preprocess_summary_path

    if not week_dir.is_dir():
        raise SystemExit(f"周目录不存在：{week_dir}")
    if not raw_dir.is_dir():
        raise SystemExit(f"原始提交目录不存在：{raw_dir}")
    if args.max_workers < 1:
        raise SystemExit("--max-workers 必须大于等于 1")

    print(f"[CONFIG] {runtime_config.assignment_id} | {runtime_config.subject.subject_name} | {runtime_config.week_name}")
    processed_dir.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    raw_zip_paths = sorted(raw_dir.glob("*.zip"))

    if not raw_zip_paths:
        summary_path = write_summary(summary_path, runtime_config.week_name, [])
        print(f"[DONE] 未发现原始提交，summary 已写入 {summary_path}")
        return 0

    results: list[StudentPreprocessResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        future_map = {
            executor.submit(preprocess_one_student, raw_zip_path, processed_dir, temp_root, args.reprocess, backup_root): raw_zip_path
            for raw_zip_path in raw_zip_paths
        }
        for future in concurrent.futures.as_completed(future_map):
            raw_zip_path = future_map[future]
            student_id = parse_student_id(raw_zip_path)
            try:
                results.append(future.result())
            except Exception as exc:
                print(f"[FAIL] {student_id} | 批量任务异常：{exc}")
                results.append(
                    StudentPreprocessResult(
                        student_id=student_id,
                        status="failed",
                        page_count=0,
                        detail=f"批量任务异常：{exc}",
                    )
                )

    results.sort(key=lambda item: item.student_id)
    summary_path = write_summary(summary_path, runtime_config.week_name, results)
    print(f"[DONE] summary 已写入 {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
