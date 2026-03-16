import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from project_config import AssignmentConfig, load_runtime_config


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


class ReviewRepository:
    def __init__(self, assignment_config: AssignmentConfig):
        self.assignment_config = assignment_config
        self.week_dir = assignment_config.week_dir.resolve()
        self.processed_dir = assignment_config.processed_images_dir
        self.results_dir = assignment_config.results_dir
        self.ui_dir = Path(__file__).resolve().parent / "review_ui"

    def list_students(self) -> list[dict]:
        students: list[dict] = []
        if not self.processed_dir.is_dir():
            return students

        for student_dir in sorted(path for path in self.processed_dir.iterdir() if path.is_dir()):
            result_path = self.results_dir / f"{student_dir.name}.txt"
            page_count = len(self.get_image_paths(student_dir.name))
            students.append(
                {
                    "id": student_dir.name,
                    "pageCount": page_count,
                    "hasResult": result_path.exists() and result_path.stat().st_size > 0,
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

    def get_student_payload(self, student_id: str) -> dict:
        result_path = self.get_result_path(student_id)
        result_text = result_path.read_text(encoding="utf-8") if result_path.exists() else ""
        image_paths = self.get_image_paths(student_id)
        images = [f"/images/{student_id}/{path.name}" for path in image_paths]
        return {
            "id": student_id,
            "images": images,
            "resultText": result_text,
        }

    def save_result(self, student_id: str, content: str) -> None:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        result_path = self.get_result_path(student_id)
        result_path.write_text(content, encoding="utf-8")

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


def create_handler(repository: ReviewRepository):
    class ReviewRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path

                if path == "/" or path == "/index.html":
                    self.serve_file(repository.resolve_ui_asset("index.html"))
                    return
                if path == "/assets/style.css":
                    self.serve_file(repository.resolve_ui_asset("style.css"))
                    return
                if path == "/assets/app.js":
                    self.serve_file(repository.resolve_ui_asset("app.js"))
                    return
                if path == "/api/students":
                    self.send_json({"students": repository.list_students()})
                    return
                if path.startswith("/api/student/"):
                    student_id = unquote(path[len("/api/student/"):])
                    self.send_json(repository.get_student_payload(student_id))
                    return
                if path.startswith("/images/"):
                    parts = [unquote(part) for part in path.split("/") if part]
                    if len(parts) != 3:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    _, student_id, file_name = parts
                    self.serve_file(repository.resolve_image(student_id, file_name))
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
                if not path.startswith("/api/student/"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return

                student_id = unquote(path[len("/api/student/"):])
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length)
                payload = json.loads(raw_body.decode("utf-8"))
                content = payload.get("content", "")
                if not isinstance(content, str):
                    self.send_error(HTTPStatus.BAD_REQUEST, "content must be string")
                    return

                repository.save_result(student_id, content)
                self.send_json({"ok": True})
            except FileNotFoundError as exc:
                self.send_error(HTTPStatus.NOT_FOUND, str(exc))
            except json.JSONDecodeError:
                self.send_error(HTTPStatus.BAD_REQUEST, "invalid json body")
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def serve_file(self, file_path: Path) -> None:
            mime_type, _ = mimetypes.guess_type(str(file_path))
            body = file_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            return

    return ReviewRequestHandler


def main() -> int:
    args = parse_args()
    try:
        assignment_config = load_runtime_config(assignment=args.assignment, week=args.week)
    except ValueError as exc:
        raise SystemExit(str(exc))
    week_dir = assignment_config.week_dir
    if not week_dir.is_dir():
        raise SystemExit(f"周目录不存在：{week_dir}")

    repository = ReviewRepository(assignment_config)
    if not repository.processed_dir.is_dir():
        raise SystemExit(f"标准化图片目录不存在：{repository.processed_dir}")

    handler = create_handler(repository)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(
        f"Review app running at http://{args.host}:{args.port} | "
        f"{assignment_config.assignment_id} | {assignment_config.subject.subject_name} | {assignment_config.week_name}"
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
