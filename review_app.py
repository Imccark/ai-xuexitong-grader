import argparse
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tempfile
import uuid
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import fitz
from PIL import Image

from create_week import build_assignment_payload, write_json
from project_config import (
    LOCAL_ENV_FILE,
    LOCAL_SETTINGS_FILE,
    AssignmentConfig,
    get_export_engine_setting,
    get_local_env_var,
    is_valid_env_name,
    list_assignment_config_paths,
    load_assignment_config,
    load_runtime_config,
    write_export_engine_setting,
    write_local_env_var,
)
from run_batch_grading import parse_result_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地作业审阅前端。")
    parser.add_argument("--week", help="周目录，例如：第一周；如果仓库里只有一个 assignment，可省略")
    parser.add_argument("--assignment", help="assignment 配置文件路径；如果仓库里只有一个 assignment，可省略")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    return parser.parse_args()


def get_page_number(path: Path) -> int:
    stem = path.stem
    if "_" not in stem:
        return 0
    try:
        return int(stem.rsplit("_", 1)[1])
    except ValueError:
        return 0


class ExportImageError(RuntimeError):
    """Raised when the backend export pipeline cannot render a PNG."""


_EXPORT_WORKER_COUNT = 2
_EXPORT_IMAGE_RENDER_SCALE = 2.2
_LATEX_TEXT_ESCAPES = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "#": r"\#",
    "$": r"\$",
    "%": r"\%",
    "&": r"\&",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_LATEX_REQUIRED_FILES = ["standalone.cls", "ctex.sty", "amsmath.sty", "amssymb.sty", "bm.sty"]


def _escape_latex_text(text: str) -> str:
    return "".join(_LATEX_TEXT_ESCAPES.get(char, char) for char in text)


def _detect_runtime_platform() -> dict:
    runtime_platform = str(sys.platform or "").lower()
    system_name = "Linux"
    if runtime_platform.startswith("win") or os.name == "nt":
        system_name = "Windows"
    elif runtime_platform == "darwin":
        system_name = "macOS"

    is_wsl = False
    try:
        version_text = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
        is_wsl = "microsoft" in version_text or "wsl" in version_text
    except Exception:
        is_wsl = False

    label = "WSL" if is_wsl else system_name
    return {
        "system": system_name,
        "label": label,
        "isWsl": is_wsl,
        "platform": runtime_platform,
    }


def _latex_install_hint(platform_info: dict) -> str:
    label = str(platform_info.get("label") or "")
    if label == "Windows":
        return "Windows 建议安装 MiKTeX 或 TeX Live，并确保 lualatex 在 PATH 中。"
    if label == "macOS":
        return "macOS 建议安装 MacTeX，并确保 lualatex 可在终端直接执行。"
    if label == "WSL":
        return "当前是 WSL，需在 WSL 里的 Linux 环境安装 TeX Live，而不是只装 Windows 侧 LaTeX。"
    return "Linux 建议安装 TeX Live，并确保 lualatex 与 kpsewhich 在 PATH 中。"


def _get_latex_environment_status() -> dict:
    platform_info = _detect_runtime_platform()
    lualatex_path = shutil.which("lualatex") or ""
    kpsewhich_path = shutil.which("kpsewhich") or ""
    missing_files: list[str] = []

    if kpsewhich_path:
        for tex_file in _LATEX_REQUIRED_FILES:
            try:
                completed = subprocess.run(
                    ["kpsewhich", tex_file],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=8,
                    check=False,
                )
            except Exception:
                missing_files.append(tex_file)
                continue
            if completed.returncode != 0 or not completed.stdout.strip():
                missing_files.append(tex_file)

    available = bool(lualatex_path) and (not kpsewhich_path or not missing_files)
    detail_parts = []
    if not lualatex_path:
        detail_parts.append("未找到 lualatex")
    if not kpsewhich_path:
        detail_parts.append("未找到 kpsewhich，无法校验宏包")
    elif missing_files:
        detail_parts.append(f"缺少宏包文件：{', '.join(missing_files)}")

    return {
        "available": available,
        "platform": platform_info,
        "lualatexPath": lualatex_path,
        "kpsewhichPath": kpsewhich_path,
        "missingFiles": missing_files,
        "detail": "；".join(detail_parts),
        "hint": _latex_install_hint(platform_info),
    }


def _is_escaped(source: str, index: int) -> bool:
    backslash_count = 0
    cursor = index - 1
    while cursor >= 0 and source[cursor] == "\\":
        backslash_count += 1
        cursor -= 1
    return backslash_count % 2 == 1


def _find_math_closer(source: str, start: int, opener: str, closer: str) -> int:
    cursor = start
    while cursor < len(source):
        if source.startswith(closer, cursor) and not _is_escaped(source, cursor):
            if closer == "$" and opener == "$" and source.startswith("$$", cursor):
                cursor += 1
                continue
            return cursor
        cursor += 1
    return -1


def _tokenize_export_text(source: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    text_buffer: list[str] = []
    cursor = 0

    def flush_text() -> None:
        if text_buffer:
            tokens.append(("text", "".join(text_buffer)))
            text_buffer.clear()

    while cursor < len(source):
        if source.startswith("$$", cursor) and not _is_escaped(source, cursor):
            closing = _find_math_closer(source, cursor + 2, "$$", "$$")
            if closing >= 0:
                flush_text()
                tokens.append(("display_math", source[cursor + 2:closing]))
                cursor = closing + 2
                continue
        if source.startswith(r"\[", cursor):
            closing = _find_math_closer(source, cursor + 2, r"\[", r"\]")
            if closing >= 0:
                flush_text()
                tokens.append(("display_math", source[cursor + 2:closing]))
                cursor = closing + 2
                continue
        if source.startswith(r"\(", cursor):
            closing = _find_math_closer(source, cursor + 2, r"\(", r"\)")
            if closing >= 0:
                flush_text()
                tokens.append(("inline_math", source[cursor + 2:closing]))
                cursor = closing + 2
                continue
        if source[cursor] == "$" and not _is_escaped(source, cursor):
            closing = _find_math_closer(source, cursor + 1, "$", "$")
            if closing >= 0:
                flush_text()
                tokens.append(("inline_math", source[cursor + 1:closing]))
                cursor = closing + 1
                continue

        text_buffer.append(source[cursor])
        cursor += 1

    flush_text()
    return tokens


def _render_export_text_to_latex_body(source: str) -> str:
    parts: list[str] = []
    for token_type, value in _tokenize_export_text(source.replace("\r\n", "\n").replace("\r", "\n")):
        if token_type == "text":
            for segment in re.split("(\n)", value):
                if not segment:
                    continue
                if segment == "\n":
                    parts.append(r"\par" + "\n")
                else:
                    parts.append(_escape_latex_text(segment))
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        if token_type == "display_math":
            parts.append("\n\\[\n" + cleaned + "\n\\]\n")
        else:
            parts.append(f"${cleaned}$")

    body = "".join(parts).strip()
    if not body:
        raise ExportImageError("导出内容为空，无法生成图片。")
    return body


def _build_export_latex_document(source: str) -> str:
    body = _render_export_text_to_latex_body(source)
    return (
        r"\documentclass[border=12pt]{standalone}" "\n"
        r"\usepackage{ctex}" "\n"
        r"\usepackage{amsmath,amssymb,bm}" "\n"
        r"\pagestyle{empty}" "\n"
        r"\begin{document}" "\n"
        r"\begin{minipage}{170mm}" "\n"
        r"\setlength{\parindent}{0pt}" "\n"
        r"\setlength{\parskip}{4pt}" "\n"
        r"\raggedright" "\n"
        r"\small" "\n"
        + body
        + "\n"
        + r"\end{minipage}" "\n"
        + r"\end{document}" "\n"
    )


def _render_pdf_to_png_bytes(pdf_path: Path) -> bytes:
    document = fitz.open(pdf_path)
    images: list[Image.Image] = []
    try:
        for page in document:
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(_EXPORT_IMAGE_RENDER_SCALE, _EXPORT_IMAGE_RENDER_SCALE),
                alpha=False,
            )
            image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
            images.append(image)
    finally:
        document.close()

    if not images:
        raise ExportImageError("导出失败：未生成 PDF 页面。")

    if len(images) == 1:
        canvas = images[0]
    else:
        gap = 24
        width = max(image.width for image in images)
        height = sum(image.height for image in images) + gap * (len(images) - 1)
        canvas = Image.new("RGB", (width, height), "white")
        offset_y = 0
        for image in images:
            canvas.paste(image, (0, offset_y))
            offset_y += image.height + gap
            image.close()

    output = io.BytesIO()
    canvas.save(output, format="PNG")
    canvas.close()
    return output.getvalue()


def render_review_text_to_png_bytes(source: str) -> bytes:
    tex_source = _build_export_latex_document(source)
    with tempfile.TemporaryDirectory(prefix="review_export_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        tex_path = temp_dir / "export.tex"
        pdf_path = temp_dir / "export.pdf"
        tex_path.write_text(tex_source, encoding="utf-8")

        command = [
            "lualatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            tex_path.name,
        ]
        completed = subprocess.run(
            command,
            cwd=temp_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=40,
            check=False,
        )
        if completed.returncode != 0 or not pdf_path.is_file():
            log_tail = "\n".join(completed.stdout.splitlines()[-25:]).strip()
            detail = f"\n{log_tail}" if log_tail else ""
            raise ExportImageError(f"LaTeX 编译失败，无法导出图片。{detail}")

        return _render_pdf_to_png_bytes(pdf_path)


class ReviewRepository:
    def __init__(self, assignment_config: AssignmentConfig):
        self.assignment_config = assignment_config
        self.week_dir = assignment_config.week_dir.resolve()
        self.processed_dir = assignment_config.processed_images_dir
        self.results_dir = assignment_config.results_dir
        self.ui_dir = Path(__file__).resolve().parent / "review_ui"
        self._export_lock = threading.Lock()
        self._export_condition = threading.Condition(self._export_lock)
        self._export_queue: deque[str] = deque()
        self._export_queued_ids: set[str] = set()
        self._export_records: dict[str, dict] = {}
        self._start_export_workers()

    def _start_export_workers(self) -> None:
        for index in range(_EXPORT_WORKER_COUNT):
            thread = threading.Thread(
                target=self._export_worker_loop,
                name=f"export-cache-{self.assignment_config.assignment_id}-{index + 1}",
                daemon=True,
            )
            thread.start()

    def _get_export_source_path(self, student_id: str) -> Path | None:
        txt_path = self.get_result_path(student_id)
        if txt_path.exists() and txt_path.stat().st_size > 0:
            return txt_path
        json_path = self.get_result_json_path(student_id)
        if json_path.exists() and json_path.stat().st_size > 0:
            return json_path
        return None

    def _get_export_source_mtime(self, student_id: str) -> float:
        source_path = self._get_export_source_path(student_id)
        if source_path is None:
            return 0.0
        try:
            return float(source_path.stat().st_mtime or 0.0)
        except Exception:
            return 0.0

    def _snapshot_export_status_locked(self, student_id: str) -> dict:
        record = self._export_records.get(student_id, {})
        source_path = self._get_export_source_path(student_id)
        source_mtime = self._get_export_source_mtime(student_id)
        cached_source_mtime = float(record.get("sourceMtime") or 0.0)
        status = str(record.get("status") or "")

        if source_path is None:
            if status not in {"queued", "rendering"}:
                status = "missing"
        elif status not in {"queued", "rendering"}:
            has_cached_source = isinstance(record.get("sourceText"), str) and bool(record.get("sourceText", "").strip())
            status = "ready" if has_cached_source and cached_source_mtime >= source_mtime - 1e-6 else "stale"

        error = str(record.get("error") or "").strip()
        if status == "error" and not error:
            status = "stale"

        return {
            "studentId": student_id,
            "status": status,
            "ready": status == "ready",
            "queued": status == "queued",
            "rendering": status == "rendering",
            "missing": status == "missing",
            "error": error,
            "sourcePath": str(source_path) if source_path else "",
            "imagePath": "",
            "imageUrl": "",
            "sourceMtime": source_mtime,
            "imageMtime": cached_source_mtime,
            "updatedAt": float(record.get("updatedAt") or 0.0),
        }

    def get_export_image_status(self, student_id: str, *, auto_enqueue: bool = False, urgent: bool = False) -> dict:
        with self._export_condition:
            status = self._snapshot_export_status_locked(student_id)
            if auto_enqueue and status["status"] in {"stale", "error"}:
                self._queue_export_render_locked(student_id, urgent=urgent)
                status = self._snapshot_export_status_locked(student_id)
            return dict(status)

    def _queue_export_render_locked(self, student_id: str, *, urgent: bool = False) -> dict:
        source_path = self._get_export_source_path(student_id)
        if source_path is None:
            record = self._export_records.setdefault(student_id, {})
            record["status"] = "missing"
            record["error"] = ""
            record["updatedAt"] = time.time()
            return self._snapshot_export_status_locked(student_id)

        record = self._export_records.setdefault(student_id, {})
        record["error"] = ""
        record["updatedAt"] = time.time()

        if record.get("status") == "rendering":
            record["needsRerun"] = True
            return self._snapshot_export_status_locked(student_id)

        if student_id in self._export_queued_ids:
            if urgent:
                try:
                    self._export_queue.remove(student_id)
                except ValueError:
                    pass
                self._export_queue.appendleft(student_id)
        else:
            if urgent:
                self._export_queue.appendleft(student_id)
            else:
                self._export_queue.append(student_id)
            self._export_queued_ids.add(student_id)
        record["status"] = "queued"
        self._export_condition.notify()
        return self._snapshot_export_status_locked(student_id)

    def queue_export_render(self, student_id: str, *, urgent: bool = False) -> dict:
        with self._export_condition:
            return self._queue_export_render_locked(student_id, urgent=urgent)

    def _export_worker_loop(self) -> None:
        while True:
            with self._export_condition:
                while not self._export_queue:
                    self._export_condition.wait()
                student_id = self._export_queue.popleft()
                self._export_queued_ids.discard(student_id)
                record = self._export_records.setdefault(student_id, {})
                record["status"] = "rendering"
                record["error"] = ""
                record["updatedAt"] = time.time()
                render_source_mtime = self._get_export_source_mtime(student_id)

            success = False
            error_message = ""
            source_text = ""
            try:
                source_text = self.build_export_image_source(student_id)
                success = True
            except Exception as exc:
                error_message = str(exc)

            with self._export_condition:
                record = self._export_records.setdefault(student_id, {})
                latest_source_mtime = self._get_export_source_mtime(student_id)
                needs_rerun = bool(record.get("needsRerun")) or (
                    success and latest_source_mtime > render_source_mtime + 1e-6
                )
                record["needsRerun"] = False
                record["updatedAt"] = time.time()

                if success and not needs_rerun:
                    record["status"] = "ready"
                    record["error"] = ""
                    record["sourceText"] = source_text
                    record["sourceMtime"] = render_source_mtime
                elif needs_rerun:
                    if student_id not in self._export_queued_ids:
                        self._export_queue.appendleft(student_id)
                        self._export_queued_ids.add(student_id)
                    record["status"] = "queued"
                    record["error"] = ""
                    self._export_condition.notify()
                else:
                    record["status"] = "error"
                    record["error"] = error_message
                    record["sourceText"] = ""
                self._export_condition.notify_all()

    def wait_for_export_image(self, student_id: str, timeout_seconds: float = 0.0) -> dict:
        deadline = time.time() + max(0.0, timeout_seconds)
        with self._export_condition:
            status = self._snapshot_export_status_locked(student_id)
            if status["status"] in {"stale", "error"}:
                self._queue_export_render_locked(student_id, urgent=True)
                status = self._snapshot_export_status_locked(student_id)

            while timeout_seconds > 0 and status["status"] in {"queued", "rendering"}:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._export_condition.wait(timeout=remaining)
                status = self._snapshot_export_status_locked(student_id)
            return dict(status)

    def list_students(self) -> list[dict]:
        students: list[dict] = []
        if not self.processed_dir.is_dir():
            return students

        for student_dir in sorted(path for path in self.processed_dir.iterdir() if path.is_dir()):
            result_txt_path = self.results_dir / f"{student_dir.name}.txt"
            result_json_path = self.results_dir / f"{student_dir.name}.json"
            page_count = len(self.get_image_paths(student_dir.name))
            students.append(
                {
                    "id": student_dir.name,
                    "pageCount": page_count,
                    "hasResult": (
                        (result_json_path.exists() and result_json_path.stat().st_size > 0)
                        or (result_txt_path.exists() and result_txt_path.stat().st_size > 0)
                    ),
                    "hasExportImage": False,
                }
            )
        return students

    def get_image_paths(self, student_id: str) -> list[Path]:
        student_dir = self.processed_dir / student_id
        if not student_dir.is_dir():
            raise FileNotFoundError(f"学生目录不存在：{student_id}")
        return sorted(
            (path for path in student_dir.iterdir() if path.is_file() and path.suffix.lower() == ".png"),
            key=get_page_number,
        )

    def get_result_path(self, student_id: str) -> Path:
        return self.results_dir / f"{student_id}.txt"

    def get_result_json_path(self, student_id: str) -> Path:
        return self.results_dir / f"{student_id}.json"

    def load_result_json(self, student_id: str) -> dict:
        json_path = self.get_result_json_path(student_id)
        if json_path.exists() and json_path.stat().st_size > 0:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            return self.enrich_result_json(payload)

        txt_path = self.get_result_path(student_id)
        txt_content = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""
        if txt_content.strip():
            return parse_result_text(txt_content, self.assignment_config.subject.output_format)
        return {
            "student_name_or_id": student_id,
            "overall": "",
            "modules": {},
            "error_details_by_question": {},
            "proof_review_by_question": {},
        }

    def enrich_result_json(self, result_json: dict, rendered_text: str | None = None) -> dict:
        safe_payload = result_json if isinstance(result_json, dict) else {}
        text = rendered_text if isinstance(rendered_text, str) else self.render_result_text(safe_payload)
        parsed = parse_result_text(text, self.assignment_config.subject.output_format)

        student_name = safe_payload.get("student_name_or_id")
        overall = safe_payload.get("overall")
        modules = safe_payload.get("modules")

        return {
            "student_name_or_id": student_name if isinstance(student_name, str) else parsed.get("student_name_or_id", ""),
            "overall": overall if isinstance(overall, str) else parsed.get("overall", ""),
            "modules": modules if isinstance(modules, dict) else parsed.get("modules", {}),
            "error_details_by_question": parsed.get("error_details_by_question", {}),
            "proof_review_by_question": parsed.get("proof_review_by_question", {}),
        }

    def render_result_text(self, result_json: dict) -> str:
        student_name = result_json.get("student_name_or_id", "")
        overall = result_json.get("overall", "")
        modules = result_json.get("modules", {})
        lines = [
            "========================================",
            f"姓名/学号：{student_name}",
            f"整体情况：{overall}",
        ]
        if isinstance(modules, dict):
            for title, block in modules.items():
                lines.append(f"{title}：")
                items: list[str] = []
                if isinstance(block, dict):
                    raw_items = block.get("items", [])
                    if isinstance(raw_items, list):
                        items = [str(item).strip() for item in raw_items if str(item).strip()]
                    if not items:
                        raw_text = str(block.get("raw_text", "")).strip()
                        if raw_text:
                            items = [raw_text]
                elif isinstance(block, str) and block.strip():
                    items = [block.strip()]
                if items:
                    for index, item in enumerate(items, start=1):
                        lines.append(f"{index}. {item}")
                else:
                    lines.append("1. ")
        lines.append("========================================")
        return "\n".join(lines) + "\n"

    def get_student_payload(self, student_id: str) -> dict:
        image_paths = self.get_image_paths(student_id)
        images = [f"/images/{student_id}/{path.name}" for path in image_paths]
        result_json = self.load_result_json(student_id)
        return {
            "id": student_id,
            "images": images,
            "resultJson": result_json,
            "exportImage": self.get_export_image_status(student_id, auto_enqueue=True, urgent=False),
        }

    def save_result(self, student_id: str, result_json: dict, rendered_text: str | None = None) -> dict:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        text = rendered_text if isinstance(rendered_text, str) else self.render_result_text(result_json)
        enriched_payload = self.enrich_result_json(result_json, rendered_text=text)
        json_path = self.get_result_json_path(student_id)
        json_path.write_text(json.dumps(enriched_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result_path = self.get_result_path(student_id)
        result_path.write_text(text, encoding="utf-8")
        return self.queue_export_render(student_id, urgent=True)

    def build_export_image_source(
        self,
        student_id: str,
        result_json: dict | None = None,
        rendered_text: str | None = None,
    ) -> str:
        if isinstance(rendered_text, str):
            source_text = rendered_text
        elif isinstance(result_json, dict):
            source_text = self.render_result_text(result_json)
        else:
            result_path = self.get_result_path(student_id)
            if result_path.exists():
                source_text = result_path.read_text(encoding="utf-8")
            else:
                source_text = self.render_result_text(self.load_result_json(student_id))
        normalized = source_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise ExportImageError("导出内容为空，无法生成图片。")
        return normalized

    def get_cached_export_source(self, student_id: str) -> str:
        with self._export_condition:
            status = self._snapshot_export_status_locked(student_id)
            if status["status"] != "ready":
                raise ExportImageError("导出图片尚未就绪，请稍候。")
            record = self._export_records.get(student_id, {})
            source_text = str(record.get("sourceText") or "")
            if not source_text.strip():
                raise ExportImageError("导出内容为空，无法生成图片。")
            return source_text

    def render_cached_export_image_bytes(self, student_id: str, engine: str) -> bytes:
        normalized_engine = str(engine or "").strip().lower()
        if normalized_engine != "latex":
            raise ExportImageError(f"不支持的服务端导出引擎：{engine}")
        source_text = self.get_cached_export_source(student_id)
        return render_review_text_to_png_bytes(source_text)

    def resolve_ui_asset(self, asset_name: str) -> Path:
        asset_path = (self.ui_dir / asset_name).resolve()
        if self.ui_dir not in asset_path.parents and asset_path != self.ui_dir:
            raise FileNotFoundError("非法资源路径")
        if not asset_path.is_file():
            raise FileNotFoundError(asset_name)
        return asset_path

    def resolve_image(self, student_id: str, file_name: str) -> Path:
        image_path = (self.processed_dir / student_id / file_name).resolve()
        student_dir = (self.processed_dir / student_id).resolve()
        if student_dir not in image_path.parents:
            raise FileNotFoundError("非法图片路径")
        if not image_path.is_file():
            raise FileNotFoundError(file_name)
        return image_path


_repository: ReviewRepository | None = None
_task_lock = threading.Lock()
_pipeline_tasks: dict[str, dict] = {}


def create_week_resources(week_name: str, force: bool = False) -> dict:
    from project_config import DEFAULT_ANSWER_KEY_FILENAME, REPO_ROOT

    resolved_week_name = week_name.strip()
    if not resolved_week_name:
        raise ValueError("week_name 不能为空。")

    new_week_dir = REPO_ROOT / resolved_week_name
    if new_week_dir.exists() and not force:
        raise ValueError(f"周目录已存在：{new_week_dir}。")

    assignment_path, assignment_payload = build_assignment_payload(
        new_week_name=resolved_week_name,
        new_week_dir=new_week_dir,
        new_answer_key_name=DEFAULT_ANSWER_KEY_FILENAME,
    )
    if assignment_path.exists() and not force:
        raise ValueError(f"assignment 配置已存在：{assignment_path}。")

    directories = [
        new_week_dir,
        new_week_dir / "raw_submissions",
        new_week_dir / "processed_images",
        new_week_dir / "results",
        new_week_dir / "temp_workspace",
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    answer_key_path = new_week_dir / DEFAULT_ANSWER_KEY_FILENAME
    if not answer_key_path.exists():
        answer_key_path.write_text("% 在这里填写本周标准答案\n", encoding="utf-8")

    for summary_path in [new_week_dir / "preprocess_summary.txt", new_week_dir / "summary.txt"]:
        if not summary_path.exists():
            summary_path.write_text("", encoding="utf-8")

    write_json(assignment_path, assignment_payload)
    return {
        "weekId": assignment_path.stem,
        "weekName": resolved_week_name,
        "assignmentPath": str(assignment_path),
        "rawSubmissionsPath": str((new_week_dir / "raw_submissions").resolve()),
        "answerKeyPath": str(answer_key_path.resolve()),
    }


def try_open_path(path: Path) -> tuple[bool, str | None]:
    try:
        runtime_platform = str(sys.platform or "").lower()
        is_wsl = "microsoft" in os.uname().release.lower() if hasattr(os, "uname") else False
        is_windows_runtime = os.name == "nt" or runtime_platform.startswith("win") or runtime_platform.startswith("msys") or runtime_platform.startswith("cygwin")
        if is_wsl:
            windows_path = str(path)
            try:
                converted = subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()
                if converted:
                    windows_path = converted
            except Exception:
                pass
            subprocess.Popen(["explorer.exe", windows_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, None
        if is_windows_runtime:
            start_error: Exception | None = None
            if hasattr(os, "startfile"):
                try:
                    os.startfile(str(path))  # type: ignore[attr-defined]
                    return True, None
                except Exception as exc:
                    start_error = exc
            for command in (["explorer.exe", str(path)], ["explorer", str(path)]):
                try:
                    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True, None
                except Exception as exc:
                    start_error = exc
            if start_error is not None:
                return False, str(start_error)
            return False, "无法调用 Windows 打开命令"
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, None
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, None
    except FileNotFoundError as exc:
        return False, f"系统缺少打开命令：{exc}"
    except Exception as exc:
        return False, str(exc)


def get_directory_created_timestamp(path: Path) -> float:
    """
    跨平台近似目录创建时间：
    - macOS: 优先 st_birthtime
    - Windows: st_ctime 即创建时间
    - Linux/WSL: 通常无 birthtime，回退到 st_mtime（创建后通常最稳定）
    """
    try:
        stat = path.stat()
    except Exception:
        return 0.0

    birthtime = getattr(stat, "st_birthtime", None)
    if isinstance(birthtime, (int, float)) and birthtime > 0:
        return float(birthtime)

    if os.name == "nt":
        return float(getattr(stat, "st_ctime", 0.0) or 0.0)

    mtime = float(getattr(stat, "st_mtime", 0.0) or 0.0)
    if mtime > 0:
        return mtime
    return float(getattr(stat, "st_ctime", 0.0) or 0.0)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _ui_root() -> Path:
    return _repo_root() / "review_ui"


def _resolve_ui_asset(asset_name: str) -> Path:
    ui_root = _ui_root().resolve()
    asset_path = (ui_root / asset_name).resolve()
    if ui_root not in asset_path.parents and asset_path != ui_root:
        raise FileNotFoundError("非法资源路径")
    if not asset_path.is_file():
        raise FileNotFoundError(asset_name)
    return asset_path


def _assignment_path_from_week_id(week_id: str) -> Path:
    assignment_path = _repo_root() / "configs" / "assignments" / f"{week_id}.json"
    if not assignment_path.is_file():
        raise FileNotFoundError(f"assignment 不存在：{assignment_path.name}")
    return assignment_path


def _pipeline_logs_dir() -> Path:
    return _repo_root() / "runtime_logs"


def _build_pipeline_command(task_name: str, assignment_path: Path, max_workers: int, flag_enabled: bool) -> list[str]:
    if task_name == "preprocess":
        cmd = [
            sys.executable,
            "-u",
            "run_preprocessing.py",
            "--assignment",
            str(assignment_path),
            "--max-workers",
            str(max_workers),
        ]
        if flag_enabled:
            cmd.append("--reprocess")
        return cmd
    if task_name == "grading":
        cmd = [
            sys.executable,
            "-u",
            "run_batch_grading.py",
            "--assignment",
            str(assignment_path),
            "--max-workers",
            str(max_workers),
        ]
        if flag_enabled:
            cmd.append("--regrade")
        return cmd
    raise ValueError(f"不支持的任务类型：{task_name}")


def _start_pipeline_task(task_name: str, week_id: str, max_workers: int, flag_enabled: bool) -> dict:
    if task_name not in {"preprocess", "grading"}:
        raise ValueError("task 仅支持 preprocess 或 grading")
    if max_workers < 1:
        raise ValueError("maxWorkers 必须大于等于 1")

    assignment_path = _assignment_path_from_week_id(week_id)
    with _task_lock:
        for existing in _pipeline_tasks.values():
            if (
                existing.get("task") == task_name
                and existing.get("weekId") == week_id
                and existing.get("status") == "running"
            ):
                raise ValueError(f"{week_id} 的 {task_name} 任务正在运行中，请勿重复启动")

    task_id = uuid.uuid4().hex[:12]
    logs_dir = _pipeline_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{task_name}_{week_id}_{task_id}.log"
    cmd = _build_pipeline_command(task_name, assignment_path, max_workers, flag_enabled)

    record = {
        "taskId": task_id,
        "task": task_name,
        "weekId": week_id,
        "status": "running",
        "maxWorkers": max_workers,
        "flagEnabled": bool(flag_enabled),
        "command": cmd,
        "logPath": str(log_path),
        "startedAt": time.time(),
        "endedAt": None,
        "returnCode": None,
        "error": None,
    }
    with _task_lock:
        _pipeline_tasks[task_id] = record

    def _runner() -> None:
        return_code = -1
        error_message: str | None = None
        try:
            with log_path.open("w", encoding="utf-8") as log_file:
                log_file.write(f"[TASK] {task_name} | week={week_id}\n")
                log_file.write(f"[CMD] {' '.join(cmd)}\n\n")
                log_file.flush()
                process = subprocess.Popen(
                    cmd,
                    cwd=str(_repo_root()),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
                return_code = process.wait()
        except Exception as exc:
            error_message = str(exc)
        finally:
            with _task_lock:
                task_record = _pipeline_tasks.get(task_id)
                if task_record is not None:
                    task_record["returnCode"] = return_code
                    task_record["endedAt"] = time.time()
                    if error_message:
                        task_record["status"] = "failed"
                        task_record["error"] = error_message
                    else:
                        task_record["status"] = "success" if return_code == 0 else "failed"
                        if return_code != 0:
                            task_record["error"] = f"退出码 {return_code}"

    threading.Thread(target=_runner, name=f"task-{task_name}-{task_id}", daemon=True).start()
    return record


def _get_latest_pipeline_task_record(task_name: str, week_id: str) -> dict | None:
    latest: dict | None = None
    with _task_lock:
        for task in _pipeline_tasks.values():
            if task.get("task") != task_name or task.get("weekId") != week_id:
                continue
            if latest is None or float(task.get("startedAt", 0)) > float(latest.get("startedAt", 0)):
                latest = dict(task)
    return latest


def _get_pipeline_task_record(task_id: str) -> dict | None:
    with _task_lock:
        task = _pipeline_tasks.get(task_id)
        return dict(task) if task else None


def _read_pipeline_log_lines(task_record: dict, since_line: int = 0, limit: int = 60) -> tuple[list[str], int]:
    log_path = Path(str(task_record.get("logPath", "")).strip())
    if not log_path.is_file():
        return [], 0
    all_lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    total = len(all_lines)
    start = max(0, min(since_line, total))
    end = min(total, start + max(1, limit))
    return all_lines[start:end], total


def create_handler():
    class ReviewRequestHandler(BaseHTTPRequestHandler):
        def _list_weeks_payload(self) -> dict:
            repo = _repository
            assignment_paths = list(Path(__file__).resolve().parent.glob("configs/assignments/*.json"))
            weeks: list[dict] = []
            for assignment_path in assignment_paths:
                week_id = assignment_path.stem
                week_name = week_id
                raw_submissions_path = ""
                answer_key_path = ""
                created_at_ts = 0.0
                try:
                    cfg = load_assignment_config(assignment_path)
                    week_name = cfg.week_name
                    raw_submissions_path = str(cfg.raw_submissions_dir.resolve())
                    answer_key_path = str(cfg.answer_key_path.resolve())
                    created_at_ts = get_directory_created_timestamp(cfg.week_dir.resolve())
                except Exception:
                    created_at_ts = get_directory_created_timestamp(assignment_path.resolve().parent)
                weeks.append(
                    {
                        "id": week_id,
                        "name": week_name,
                        "assignmentPath": str(assignment_path.resolve()),
                        "rawSubmissionsPath": raw_submissions_path,
                        "answerKeyPath": answer_key_path,
                        "createdAt": created_at_ts,
                    }
                )
            weeks.sort(key=lambda item: (float(item.get("createdAt", 0.0) or 0.0), str(item.get("id", ""))))
            current_week_id = None
            if repo and repo.assignment_config and repo.assignment_config.assignment_id:
                current_week_id = repo.assignment_config.assignment_id
            return {"weeks": weeks, "currentWeekId": current_week_id}

        def _read_prompt_file(self) -> dict:
            prompt_path = (_repo_root() / "prompts" / "default_prompt.txt").resolve()
            if not prompt_path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "prompt file not found")
                return
            content = prompt_path.read_text(encoding="utf-8")
            self.send_json({"content": content, "path": "prompts/default_prompt.txt"})

        def _write_prompt_file(self, raw_body: bytes) -> None:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length == 0:
                self.send_error(HTTPStatus.BAD_REQUEST, "empty body")
                return
            content = raw_body.decode("utf-8")
            prompt_path = (_repo_root() / "prompts" / "default_prompt.txt").resolve()
            # 安全检查：确保路径在仓库内
            repo_root = _repo_root().resolve()
            if repo_root not in prompt_path.resolve().parents and prompt_path.resolve() != repo_root:
                self.send_json({"error": "forbidden path"}, status=HTTPStatus.FORBIDDEN)
                return
            prompt_path.write_text(content, encoding="utf-8")
            self.send_json({"ok": True, "path": "prompts/default_prompt.txt"})

        def _reset_prompt_file(self) -> None:
            prompt_path = (_repo_root() / "prompts" / "default_prompt.txt").resolve()
            prompt_default_path = (_repo_root() / "prompts" / "default_prompt.default.txt").resolve()
            if not prompt_default_path.is_file():
                self.send_json({"error": "default prompt template not found"}, status=HTTPStatus.NOT_FOUND)
                return
            content = prompt_default_path.read_text(encoding="utf-8")
            prompt_path.write_text(content, encoding="utf-8")
            self.send_json({"ok": True, "content": content, "path": "prompts/default_prompt.txt"})

        def _read_subjects_json(self) -> dict:
            subjects_path = (_repo_root() / "configs" / "subjects.json").resolve()
            if not subjects_path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "subjects.json not found")
                return
            content = subjects_path.read_text(encoding="utf-8")
            self.send_json({"content": content})

        def _write_subjects_json(self, raw_body: bytes) -> None:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length == 0:
                self.send_error(HTTPStatus.BAD_REQUEST, "empty body")
                return
            try:
                json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError as exc:
                self.send_json({"error": f"JSON format error: {exc}"}, status=HTTPStatus.BAD_REQUEST)
                return
            subjects_path = (_repo_root() / "configs" / "subjects.json").resolve()
            repo_root = _repo_root().resolve()
            if repo_root not in subjects_path.resolve().parents and subjects_path.resolve() != repo_root:
                self.send_json({"error": "forbidden path"}, status=HTTPStatus.FORBIDDEN)
                return
            subjects_path.write_text(raw_body.decode("utf-8"), encoding="utf-8")
            self.send_json({"ok": True})

        def _read_api_key(self, parsed_url) -> None:
            subjects_path = (_repo_root() / "configs" / "subjects.json").resolve()
            env_name = ""
            try:
                if subjects_path.is_file():
                    subjects_data = json.loads(subjects_path.read_text(encoding="utf-8"))
                    env_name = str(subjects_data.get("api_key_env", "")).strip()
            except Exception:
                env_name = ""
            query_env = parsed_url.query
            if query_env:
                for part in query_env.split("&"):
                    if part.startswith("env="):
                        env_name = unquote(part.split("=", 1)[1]).strip() or env_name
                        break
            if not env_name:
                env_name = "DASHSCOPE_API_KEY"
            if not is_valid_env_name(env_name):
                self.send_json({"error": f"非法环境变量名：{env_name}"}, status=HTTPStatus.BAD_REQUEST)
                return
            local_value = get_local_env_var(env_name)
            self.send_json(
                {
                    "ok": True,
                    "envName": env_name,
                    "apiKey": local_value,
                    "hasApiKey": bool(local_value),
                    "storePath": str(LOCAL_ENV_FILE),
                }
            )

        def _write_api_key(self, raw_body: bytes) -> None:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json body"}, status=HTTPStatus.BAD_REQUEST)
                return
            env_name = str(payload.get("envName", "")).strip()
            api_key = str(payload.get("apiKey", "")).strip()
            if not env_name:
                self.send_json({"error": "envName 不能为空"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not is_valid_env_name(env_name):
                self.send_json({"error": f"非法环境变量名：{env_name}"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not api_key:
                self.send_json({"error": "apiKey 不能为空"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                saved_path = write_local_env_var(env_name, api_key)
                os.environ[env_name] = api_key
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            except Exception as exc:
                self.send_json({"error": f"写入失败：{exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_json(
                {
                    "ok": True,
                    "envName": env_name,
                    "hasApiKey": True,
                    "storePath": str(saved_path),
                }
            )

        def _read_export_settings(self) -> None:
            engine = get_export_engine_setting()
            latex_status = _get_latex_environment_status()
            self.send_json(
                {
                    "ok": True,
                    "exportEngine": engine,
                    "latexStatus": latex_status,
                    "storePath": str(LOCAL_SETTINGS_FILE),
                }
            )

        def _write_export_settings(self, raw_body: bytes) -> None:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json body"}, status=HTTPStatus.BAD_REQUEST)
                return
            engine = str(payload.get("exportEngine", "")).strip().lower()
            try:
                saved_path = write_export_engine_setting(engine)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            latex_status = _get_latex_environment_status()
            self.send_json(
                {
                    "ok": True,
                    "exportEngine": engine,
                    "latexStatus": latex_status,
                    "storePath": str(saved_path),
                }
            )

        def _create_week(self, raw_body: bytes) -> None:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json body"}, status=HTTPStatus.BAD_REQUEST)
                return
            week_name = str(payload.get("weekName", "")).strip()
            if not week_name:
                self.send_json({"error": "weekName 不能为空"}, status=HTTPStatus.BAD_REQUEST)
                return
            force = bool(payload.get("force", False))
            try:
                result = create_week_resources(week_name=week_name, force=force)
                self.send_json({"ok": True, **result})
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        def _delete_week(self, raw_body: bytes) -> None:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json body"}, status=HTTPStatus.BAD_REQUEST)
                return
            week_id = str(payload.get("weekId", "")).strip()
            mode = str(payload.get("mode", "assignment_only")).strip()
            confirm = str(payload.get("confirm", "")).strip()
            expected = f"DELETE ALL {week_id}" if mode == "assignment_and_week_dir" else f"DELETE {week_id}"
            if not week_id:
                self.send_json({"error": "weekId 不能为空"}, status=HTTPStatus.BAD_REQUEST)
                return
            if confirm != expected:
                self.send_json({"error": f"确认文本不匹配，请输入：{expected}"}, status=HTTPStatus.BAD_REQUEST)
                return
            assignment_path = Path(__file__).resolve().parent / "configs" / "assignments" / f"{week_id}.json"
            if not assignment_path.is_file():
                self.send_json({"error": f"assignment 不存在：{assignment_path.name}"}, status=HTTPStatus.NOT_FOUND)
                return

            week_dir_path: Path | None = None
            if mode == "assignment_and_week_dir":
                config = load_assignment_config(assignment_path)
                week_dir_path = config.week_dir.resolve()
                repo_root = Path(__file__).resolve().parent
                if repo_root not in week_dir_path.parents:
                    self.send_json({"error": f"禁止删除仓库外目录：{week_dir_path}"}, status=HTTPStatus.FORBIDDEN)
                    return

            assignment_path.unlink()
            if week_dir_path and week_dir_path.exists():
                shutil.rmtree(week_dir_path)

            global _repository
            if _repository and _repository.assignment_config.assignment_id == week_id:
                remaining = [path for path in list_assignment_config_paths() if path.stem != week_id]
                if remaining:
                    _repository = ReviewRepository(load_assignment_config(remaining[0]))
            self.send_json({"ok": True, "deletedWeekId": week_id, "scope": mode})

        def _open_week_resource(self, raw_body: bytes) -> None:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json body"}, status=HTTPStatus.BAD_REQUEST)
                return
            week_id = str(payload.get("weekId", "")).strip()
            target = str(payload.get("target", "")).strip()
            if not week_id or target not in {"raw_submissions", "answer_key"}:
                self.send_json({"error": "参数无效"}, status=HTTPStatus.BAD_REQUEST)
                return
            assignment_path = Path(__file__).resolve().parent / "configs" / "assignments" / f"{week_id}.json"
            if not assignment_path.is_file():
                self.send_json({"error": f"assignment 不存在：{assignment_path.name}"}, status=HTTPStatus.NOT_FOUND)
                return

            config = load_assignment_config(assignment_path)
            target_path = config.raw_submissions_dir.resolve() if target == "raw_submissions" else config.answer_key_path.resolve()
            repo_root = Path(__file__).resolve().parent
            if repo_root not in target_path.parents and target_path != repo_root:
                self.send_json(
                    {"error": f"禁止打开仓库外路径：{target_path}", "path": str(target_path), "opened": False},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            if not target_path.exists():
                self.send_json(
                    {"error": f"目标路径不存在：{target_path}", "path": str(target_path), "opened": False},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            opened, open_error = try_open_path(target_path)
            self.send_json(
                {
                    "ok": True,
                    "opened": opened,
                    "path": str(target_path),
                    "target": target,
                    "error": open_error,
                }
            )

        def _get_week_resource_path(self, raw_body: bytes) -> None:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json body"}, status=HTTPStatus.BAD_REQUEST)
                return
            week_id = str(payload.get("weekId", "")).strip()
            target = str(payload.get("target", "")).strip()
            if not week_id or target not in {"raw_submissions", "answer_key"}:
                self.send_json({"error": "参数无效"}, status=HTTPStatus.BAD_REQUEST)
                return
            assignment_path = Path(__file__).resolve().parent / "configs" / "assignments" / f"{week_id}.json"
            if not assignment_path.is_file():
                self.send_json({"error": f"assignment 不存在：{assignment_path.name}"}, status=HTTPStatus.NOT_FOUND)
                return

            config = load_assignment_config(assignment_path)
            target_path = config.raw_submissions_dir.resolve() if target == "raw_submissions" else config.answer_key_path.resolve()
            self.send_json({"ok": True, "path": str(target_path), "target": target})

        def _run_pipeline_task(self, raw_body: bytes) -> None:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json body"}, status=HTTPStatus.BAD_REQUEST)
                return
            task_name = str(payload.get("task", "")).strip()
            week_id = str(payload.get("weekId", "")).strip()
            max_workers = payload.get("maxWorkers", 4)
            flag_enabled = bool(payload.get("flagEnabled", False))

            if not week_id:
                self.send_json({"error": "weekId 不能为空"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                max_workers = int(max_workers)
            except (TypeError, ValueError):
                self.send_json({"error": "maxWorkers 必须是整数"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                task_record = _start_pipeline_task(task_name, week_id, max_workers, flag_enabled)
            except FileNotFoundError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"ok": True, "task": task_record})

        def _get_latest_pipeline_task(self, parsed_url) -> None:
            query = parse_qs(parsed_url.query or "")
            task_name = str((query.get("task") or [""])[0]).strip()
            week_id = str((query.get("weekId") or [""])[0]).strip()
            if task_name not in {"preprocess", "grading"}:
                self.send_json({"error": "task 参数无效"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not week_id:
                self.send_json({"error": "weekId 不能为空"}, status=HTTPStatus.BAD_REQUEST)
                return
            task_record = _get_latest_pipeline_task_record(task_name, week_id)
            self.send_json({"ok": True, "task": task_record})

        def _get_pipeline_task_detail(self, parsed_url) -> None:
            query = parse_qs(parsed_url.query or "")
            task_id = str((query.get("taskId") or [""])[0]).strip()
            since_line = str((query.get("sinceLine") or ["0"])[0]).strip()
            limit = str((query.get("limit") or ["60"])[0]).strip()
            if not task_id:
                self.send_json({"error": "taskId 不能为空"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                since_line_num = max(0, int(since_line))
                limit_num = max(1, min(200, int(limit)))
            except ValueError:
                self.send_json({"error": "sinceLine/limit 必须是整数"}, status=HTTPStatus.BAD_REQUEST)
                return
            task_record = _get_pipeline_task_record(task_id)
            if not task_record:
                self.send_json({"error": f"任务不存在：{task_id}"}, status=HTTPStatus.NOT_FOUND)
                return
            lines, total = _read_pipeline_log_lines(task_record, since_line_num, limit_num)
            self.send_json({"ok": True, "task": task_record, "lines": lines, "totalLines": total})

        def _get_export_image_status(self, student_id: str, parsed_url) -> None:
            repo = _repository
            if repo is None:
                self.send_json({"error": "当前未加载作业周，请先在控制台选择周并进入批阅"}, status=HTTPStatus.CONFLICT)
                return
            query = parse_qs(parsed_url.query or "")
            urgent = str((query.get("priority") or [""])[0]).strip().lower() in {"1", "true", "high", "urgent"}
            auto_enqueue = str((query.get("enqueue") or ["1"])[0]).strip().lower() not in {"0", "false", "no"}
            force = str((query.get("force") or [""])[0]).strip().lower() in {"1", "true", "yes", "force"}
            if force:
                status = repo.queue_export_render(student_id, urgent=True)
            else:
                status = repo.get_export_image_status(student_id, auto_enqueue=auto_enqueue, urgent=urgent)
            self.send_json({"ok": True, "exportImage": status})

        def _export_student_image(self, student_id: str) -> None:
            repo = _repository
            if repo is None:
                self.send_json({"error": "当前未加载作业周，请先在控制台选择周并进入批阅"}, status=HTTPStatus.CONFLICT)
                return

            status = repo.wait_for_export_image(student_id, timeout_seconds=1.2)
            if status["status"] != "ready":
                self.send_json(
                    {
                        "error": "导出图片尚未就绪，请稍候。",
                        "exportImage": status,
                    },
                    status=HTTPStatus.CONFLICT,
                )
                return

            engine = get_export_engine_setting()
            source_text = repo.get_cached_export_source(student_id)
            file_name = f"{student_id}-annotations.png"
            if engine == "latex":
                image_bytes = repo.render_cached_export_image_bytes(student_id, engine)
                self.send_binary(
                    image_bytes,
                    content_type="image/png",
                    file_name=file_name,
                    inline=False,
                )
                return

            self.send_json(
                {
                    "ok": True,
                    "fileName": file_name,
                    "sourceText": source_text,
                    "exportEngine": engine,
                    "exportImage": status,
                }
            )

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                repo = _repository

                if path == "/" or path == "/index.html":
                    self.serve_file(_resolve_ui_asset("index.html"))
                    return
                if path == "/assets/style.css":
                    self.serve_file(_resolve_ui_asset("style.css"))
                    return
                if path == "/assets/app.js":
                    self.serve_file(_resolve_ui_asset("app.js"))
                    return
                if path.startswith("/assets/vendor/"):
                    vendor_asset = path[len("/assets/"):]
                    self.serve_file(_resolve_ui_asset(vendor_asset))
                    return
                if path == "/api/weeks":
                    self.send_json(self._list_weeks_payload())
                    return
                if path == "/api/students":
                    if repo is None:
                        self.send_json({"students": []})
                    else:
                        self.send_json({"students": repo.list_students()})
                    return
                if path.startswith("/api/student/") and path.endswith("/export-image-status"):
                    if repo is None:
                        self.send_json({"error": "当前未加载作业周，请先在控制台选择周并进入批阅"}, status=HTTPStatus.CONFLICT)
                        return
                    student_id = unquote(path[len("/api/student/"):-len("/export-image-status")].rstrip("/"))
                    if not student_id:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    self._get_export_image_status(student_id, parsed)
                    return
                if path == "/api/student/":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if path.startswith("/api/student/"):
                    if repo is None:
                        self.send_json({"error": "当前未加载作业周，请先在控制台选择周并进入批阅"}, status=HTTPStatus.CONFLICT)
                        return
                    student_id = unquote(path[len("/api/student/"):])
                    self.send_json(repo.get_student_payload(student_id))
                    return
                if path.startswith("/api/switch-week/"):
                    week = unquote(path[len("/api/switch-week/"):])
                    self._switch_week(week)
                    return
                if path == "/api/prompt":
                    self._read_prompt_file()
                    return
                if path == "/api/subjects":
                    self._read_subjects_json()
                    return
                if path == "/api/apikey":
                    self._read_api_key(parsed)
                    return
                if path == "/api/export-settings":
                    self._read_export_settings()
                    return
                if path == "/api/pipeline/latest":
                    self._get_latest_pipeline_task(parsed)
                    return
                if path == "/api/pipeline/task":
                    self._get_pipeline_task_detail(parsed)
                    return
                if path.startswith("/images/"):
                    if repo is None:
                        self.send_error(HTTPStatus.NOT_FOUND, "当前未加载作业周")
                        return
                    parts = [unquote(part) for part in path.split("/") if part]
                    if len(parts) != 3:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    _, student_id, file_name = parts
                    self.serve_file(repo.resolve_image(student_id, file_name))
                    return
                if path.startswith("/results-images/"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return

                self.send_error(HTTPStatus.NOT_FOUND)
            except FileNotFoundError as exc:
                self.send_error(HTTPStatus.NOT_FOUND, str(exc))
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path

                if path == "/api/prompt":
                    content_length = int(self.headers.get("Content-Length", "0"))
                    raw_body = self.rfile.read(content_length) if content_length > 0 else b""
                    self._write_prompt_file(raw_body)
                    return
                if path == "/api/prompt/reset":
                    self._reset_prompt_file()
                    return

                if path == "/api/subjects":
                    content_length = int(self.headers.get("Content-Length", "0"))
                    raw_body = self.rfile.read(content_length) if content_length > 0 else b""
                    self._write_subjects_json(raw_body)
                    return
                if path == "/api/apikey":
                    content_length = int(self.headers.get("Content-Length", "0"))
                    raw_body = self.rfile.read(content_length) if content_length > 0 else b""
                    self._write_api_key(raw_body)
                    return
                if path == "/api/export-settings":
                    content_length = int(self.headers.get("Content-Length", "0"))
                    raw_body = self.rfile.read(content_length) if content_length > 0 else b""
                    self._write_export_settings(raw_body)
                    return
                if path == "/api/weeks/create":
                    content_length = int(self.headers.get("Content-Length", "0"))
                    raw_body = self.rfile.read(content_length) if content_length > 0 else b""
                    self._create_week(raw_body)
                    return
                if path == "/api/weeks/delete":
                    content_length = int(self.headers.get("Content-Length", "0"))
                    raw_body = self.rfile.read(content_length) if content_length > 0 else b""
                    self._delete_week(raw_body)
                    return
                if path == "/api/weeks/open":
                    content_length = int(self.headers.get("Content-Length", "0"))
                    raw_body = self.rfile.read(content_length) if content_length > 0 else b""
                    self._open_week_resource(raw_body)
                    return
                if path == "/api/weeks/path":
                    content_length = int(self.headers.get("Content-Length", "0"))
                    raw_body = self.rfile.read(content_length) if content_length > 0 else b""
                    self._get_week_resource_path(raw_body)
                    return
                if path == "/api/pipeline/run":
                    content_length = int(self.headers.get("Content-Length", "0"))
                    raw_body = self.rfile.read(content_length) if content_length > 0 else b""
                    self._run_pipeline_task(raw_body)
                    return

                if path.startswith("/api/student/") and path.endswith("/export-image"):
                    student_id = unquote(path[len("/api/student/"):-len("/export-image")].rstrip("/"))
                    if not student_id:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    self._export_student_image(student_id)
                    return

                if not path.startswith("/api/student/"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return

                repo = _repository
                if repo is None:
                    self.send_json({"error": "当前未加载作业周，请先在控制台选择周并进入批阅"}, status=HTTPStatus.CONFLICT)
                    return

                student_id = unquote(path[len("/api/student/"):])
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length)
                payload = json.loads(raw_body.decode("utf-8"))
                result_json = payload.get("resultJson")
                rendered_text = payload.get("renderedText")
                if not isinstance(result_json, dict):
                    self.send_error(HTTPStatus.BAD_REQUEST, "resultJson must be object")
                    return
                if rendered_text is not None and not isinstance(rendered_text, str):
                    self.send_error(HTTPStatus.BAD_REQUEST, "renderedText must be string")
                    return

                export_status = repo.save_result(student_id, result_json, rendered_text)
                self.send_json({"ok": True, "exportImage": export_status})
            except FileNotFoundError as exc:
                self.send_error(HTTPStatus.NOT_FOUND, str(exc))
            except ExportImageError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except json.JSONDecodeError:
                self.send_error(HTTPStatus.BAD_REQUEST, "invalid json body")
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def _switch_week(self, week: str) -> None:
            global _repository
            try:
                assignment_path = Path(__file__).resolve().parent / "configs" / "assignments" / f"{week}.json"
                if assignment_path.is_file():
                    config = load_assignment_config(assignment_path)
                else:
                    config = load_runtime_config(week=week)
                _repository = ReviewRepository(config)
                self.send_json({"ok": True, "week": week, "week_name": config.week_name})
            except ValueError as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))

        def serve_file(self, file_path: Path) -> None:
            mime_type, _ = mimetypes.guess_type(str(file_path))
            body = file_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_binary(
            self,
            body: bytes,
            *,
            content_type: str,
            file_name: str | None = None,
            inline: bool = True,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if file_name:
                disposition = "inline" if inline else "attachment"
                quoted_name = quote(file_name)
                self.send_header(
                    "Content-Disposition",
                    f"{disposition}; filename*=UTF-8''{quoted_name}",
                )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            return

    return ReviewRequestHandler


def main() -> int:
    args = parse_args()
    assignment_config: AssignmentConfig | None = None
    try:
        assignment_config = load_runtime_config(assignment=args.assignment, week=args.week)
    except ValueError as exc:
        if args.assignment or args.week:
            raise SystemExit(str(exc))
        assignment_paths = list_assignment_config_paths()
        if assignment_paths:
            assignment_config = load_assignment_config(sorted(assignment_paths)[0])

    global _repository
    _repository = None
    if assignment_config is not None:
        week_dir = assignment_config.week_dir
        if week_dir.is_dir():
            _repository = ReviewRepository(assignment_config)
        else:
            print(f"[WARN] 周目录不存在，先以控制台模式启动：{week_dir}")

    handler = create_handler()
    server = ThreadingHTTPServer((args.host, args.port), handler)
    if assignment_config is not None:
        print(
            f"Review app running at http://{args.host}:{args.port} | "
            f"{assignment_config.assignment_id} | {assignment_config.subject.subject_name} | {assignment_config.week_name}"
        )
    else:
        print(
            f"Review app running at http://{args.host}:{args.port} | "
            "未加载作业周（可在控制台新增/选择周后进入批阅）"
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
