import base64
import io
import os
import re
import sys
from pathlib import Path

from project_config import LOCAL_ENV_FILE, SubjectConfig, load_runtime_config, load_subject_config, resolve_api_key


DEFAULT_OUTPUT_DIR_NAME = "results"
MAX_DATA_URI_ITEM_BYTES = 10 * 1024 * 1024
SAFE_RAW_IMAGE_BYTES = 7 * 1024 * 1024


def sanitize_filename(name: str) -> str:
    """
    将学生标识清洗为安全文件名，避免路径字符问题。
    """
    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name or "unknown_student"


def get_student_result_path(student_name: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    safe_name = sanitize_filename(student_name)
    return os.path.join(output_dir, f"{safe_name}.txt")


def get_mime_type(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    mime_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".pdf": "application/pdf",
    }
    return mime_types.get(ext, "image/jpeg")


def build_data_uri(file_bytes: bytes, mime_type: str) -> str:
    b64_data = base64.b64encode(file_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{b64_data}"


def compress_image_for_data_uri(file_path: str) -> tuple[bytes, str]:
    from PIL import Image

    with Image.open(file_path) as image:
        image.load()
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        elif image.mode == "L":
            image = image.convert("RGB")

        best_bytes: bytes | None = None
        size_candidates = [None, 2800, 2400, 2000, 1600]
        quality_candidates = [85, 75, 65, 55, 45]

        for max_side in size_candidates:
            candidate_image = image.copy()
            if max_side:
                candidate_image.thumbnail((max_side, max_side))

            for quality in quality_candidates:
                buffer = io.BytesIO()
                candidate_image.save(
                    buffer,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                )
                candidate_bytes = buffer.getvalue()
                if best_bytes is None or len(candidate_bytes) < len(best_bytes):
                    best_bytes = candidate_bytes
                if len(candidate_bytes) <= SAFE_RAW_IMAGE_BYTES:
                    return candidate_bytes, "image/jpeg"

        if best_bytes is None:
            raise ValueError("图片压缩失败，未生成可上传内容。")
        return best_bytes, "image/jpeg"


def get_data_uri(file_path: str) -> str:
    mime_type = get_mime_type(file_path)
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    if len(file_bytes) > SAFE_RAW_IMAGE_BYTES:
        file_bytes, mime_type = compress_image_for_data_uri(file_path)

    data_uri = build_data_uri(file_bytes, mime_type)
    if len(data_uri.encode("utf-8")) > MAX_DATA_URI_ITEM_BYTES:
        raise ValueError(
            "图片经压缩后仍超过 data URI 单项限制（10MB），请改用更小图片或公网 URL。"
        )
    return data_uri


def build_failure_result(student_name: str, overall: str, error_lines: list[str], advice_lines: list[str]) -> str:
    error_block = "\n".join(f"{i+1}. {line}" for i, line in enumerate(error_lines))
    advice_block = "\n".join(f"{i+1}. {line}" for i, line in enumerate(advice_lines))

    return (
        "========================================\n"
        f"姓名/学号：{student_name}\n"
        f"整体情况：{overall}\n"
        "错误细节：\n"
        f"{error_block}\n"
        "证明题审查：\n"
        "1. 未进入有效判卷流程，本项待人工复核。\n"
        "改进建议：\n"
        f"{advice_block}\n"
        "========================================\n"
    )


def ensure_result_dir(output_dir: str | None, tex_path: str) -> str:
    if output_dir:
        return output_dir
    week_dir = Path(tex_path).resolve().parent
    return str(week_dir / DEFAULT_OUTPUT_DIR_NAME)


def write_result_file(result_path: str, result_text: str) -> None:
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    with open(result_path, "w", encoding="utf-8") as f:
        f.write(result_text)


def write_failure_result(
    student_name: str,
    output_dir: str,
    overall: str,
    error_lines: list[str],
    advice_lines: list[str],
) -> str:
    result_path = get_student_result_path(student_name, output_dir)
    result_text = build_failure_result(student_name, overall, error_lines, advice_lines)
    write_result_file(result_path, result_text)
    return result_path


def load_standard_answer(tex_path: str) -> str:
    with open(tex_path, "r", encoding="utf-8") as f:
        return f.read()


def load_prompt_template(prompt_template_path: Path) -> str:
    return prompt_template_path.read_text(encoding="utf-8")


def build_grading_prompt(
    subject_config: SubjectConfig,
    standard_answer: str,
    student_name: str,
) -> str:
    template = load_prompt_template(subject_config.prompt_template_path)
    return template.format(
        subject_name=subject_config.subject_name,
        standard_answer=standard_answer,
        grading_requirements=subject_config.grading_requirements,
        output_format=subject_config.output_format.format(student_name=student_name),
        student_name=student_name,
    )


def evaluate_homework_qwen_vision(
    tex_path: str,
    student_name: str,
    image_paths: list[str],
    output_dir: str | None = None,
    subject_config: SubjectConfig | None = None,
    show_reasoning: bool = False,
    show_final_result: bool = False,
    reasoning_log_path: str | None = None,
) -> bool:
    resolved_subject_config = subject_config or load_subject_config()
    api_key, api_key_source = resolve_api_key(resolved_subject_config.api_key_env)
    if api_key and api_key_source == "local_env":
        os.environ[resolved_subject_config.api_key_env] = api_key
    resolved_output_dir = ensure_result_dir(output_dir, tex_path)
    result_path = get_student_result_path(student_name, resolved_output_dir)

    if not api_key:
        write_failure_result(
            student_name,
            resolved_output_dir,
            "判卷脚本调用失败，需人工复核",
            [f"缺失 {resolved_subject_config.api_key_env}。"],
            [
                f"请先在控制台配置 API Key，或设置系统环境变量 {resolved_subject_config.api_key_env}。"
                f"本地环境文件：{LOCAL_ENV_FILE}"
            ],
        )
        print(f"[ERROR] {student_name} 缺失 API Key")
        return False

    try:
        standard_answer = load_standard_answer(tex_path)
    except Exception as e:
        write_failure_result(
            student_name,
            resolved_output_dir,
            "判卷脚本调用失败，需人工复核",
            [f"无法读取标准答案文件：{e}"],
            ["请检查 Lec1.tex 路径、编码和文件完整性后重试。"],
        )
        print(f"[ERROR] {student_name} 无法读取标准答案")
        return False

    content_list = [{"type": "text", "text": build_grading_prompt(resolved_subject_config, standard_answer, student_name)}]

    valid_image_count = 0
    for img_path in image_paths:
        if not os.path.exists(img_path):
            print(f"[WARN] 图片不存在，已跳过: {img_path}")
            continue
        try:
            data_uri = get_data_uri(img_path)
            content_list.append({
                "type": "image_url",
                "image_url": {"url": data_uri}
            })
            valid_image_count += 1
        except Exception as e:
            print(f"[WARN] 图片处理失败: {img_path} | {e}")

    if valid_image_count == 0:
        write_failure_result(
            student_name,
            resolved_output_dir,
            "图片缺失，需人工复核",
            ["没有有效图片输入，无法进入视觉判卷流程。"],
            ["请检查归档图片是否存在、是否损坏，并重新处理该学生作业。"],
        )
        print(f"[ERROR] {student_name} 没有有效图片输入")
        return False

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=resolved_subject_config.base_url,
        )

        print(f"[INFO] 开始批阅：{student_name}")

        completion = client.chat.completions.create(
            model=resolved_subject_config.model,
            messages=[{"role": "user", "content": content_list}],
            extra_body={"enable_thinking": True},
            stream=True,
        )

        final_content = ""
        reasoning_chunks = []

        for chunk in completion:
            delta = chunk.choices[0].delta

            if hasattr(delta, "reasoning_content") and delta.reasoning_content is not None:
                if show_reasoning:
                    print(delta.reasoning_content, end="", flush=True)
                elif reasoning_log_path:
                    reasoning_chunks.append(delta.reasoning_content)

            if hasattr(delta, "content") and delta.content is not None:
                final_content += delta.content

        if reasoning_log_path and reasoning_chunks:
            os.makedirs(os.path.dirname(reasoning_log_path), exist_ok=True)
            with open(reasoning_log_path, "w", encoding="utf-8") as rf:
                rf.write("".join(reasoning_chunks))

        final_content = final_content.strip()
        if not final_content:
            write_failure_result(
                student_name,
                resolved_output_dir,
                "判卷结果为空，需人工复核",
                ["视觉模型未返回有效的正式批改结果。"],
                ["请检查图片质量、接口状态和网络情况后重新处理。"],
            )
            print(f"[ERROR] {student_name} 未返回有效批改结果")
            return False

        write_result_file(result_path, final_content + "\n")

        if show_final_result:
            print(final_content)

        print(f"[INFO] {student_name} 批改完成，结果已保存到 {result_path}")
        return True

    except Exception as e:
        write_failure_result(
            student_name,
            resolved_output_dir,
            "判卷脚本调用失败，需人工复核",
            [f"接口调用失败：{str(e)}"],
            ["请检查接口配置、网络状态和输入文件后重新处理。"],
        )
        print(f"[ERROR] {student_name} 批改失败")
        return False


def parse_cli_args() -> tuple[str, str, list[str], str | None, SubjectConfig]:
    if len(sys.argv) < 4:
        print(
            "用法: python grade_evaluator.py <标准答案.tex> <学生姓名> <图片路径1> [图片路径2 ...]\n"
            "或:   python grade_evaluator.py --assignment <assignment.json> <学生姓名> <图片路径1> [图片路径2 ...]"
        )
        raise SystemExit(1)

    if sys.argv[1] == "--assignment":
        if len(sys.argv) < 5:
            raise SystemExit("使用 --assignment 时，必须提供配置路径、学生标识和图片路径。")
        assignment_config = load_runtime_config(assignment=sys.argv[2])
        return (
            str(assignment_config.answer_key_path),
            sys.argv[3],
            sys.argv[4:],
            str(assignment_config.results_dir),
            assignment_config.subject,
        )

    tex_file = sys.argv[1]
    student_name = sys.argv[2]
    image_paths = sys.argv[3:]
    default_subject = load_subject_config()
    return tex_file, student_name, image_paths, None, default_subject


if __name__ == "__main__":
    tex_file, stu_name, imgs, cli_output_dir, cli_subject_config = parse_cli_args()

    success = evaluate_homework_qwen_vision(
        tex_file,
        stu_name,
        imgs,
        output_dir=cli_output_dir,
        subject_config=cli_subject_config,
        show_reasoning=False,
        show_final_result=False,
        reasoning_log_path=None,
    )
    sys.exit(0 if success else 1)
