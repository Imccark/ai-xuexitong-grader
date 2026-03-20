import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_SUBJECT_CONFIG = REPO_ROOT / "configs" / "subjects.json"
ASSIGNMENTS_DIR = REPO_ROOT / "configs" / "assignments"
DEFAULT_ANSWER_KEY_FILENAME = "answer.tex"
LOCAL_ENV_DIR = REPO_ROOT / "configs" / "env"
LOCAL_ENV_FILE = LOCAL_ENV_DIR / "local.env"
LOCAL_SETTINGS_FILE = LOCAL_ENV_DIR / "review_ui_settings.json"
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EXPORT_ENGINE_VALUES = {"latex", "katex"}


@dataclass
class SubjectConfig:
    subject_id: str
    subject_name: str
    model: str
    base_url: str
    api_key_env: str
    prompt_template_path: Path
    grading_requirements: str
    output_format: str


@dataclass
class AssignmentConfig:
    assignment_id: str
    week_name: str
    week_dir: Path
    raw_submissions_dir: Path
    processed_images_dir: Path
    results_dir: Path
    answer_key_path: Path
    preprocess_summary_path: Path
    grading_summary_path: Path
    subject: SubjectConfig


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def is_valid_env_name(name: str) -> bool:
    return bool(ENV_NAME_PATTERN.match(name))


def _strip_wrapped_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_local_env() -> dict[str, str]:
    if not LOCAL_ENV_FILE.is_file():
        return {}
    env_map: dict[str, str] = {}
    for raw_line in LOCAL_ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not is_valid_env_name(key):
            continue
        env_map[key] = _strip_wrapped_quotes(raw_value.strip())
    return env_map


def _encode_env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_local_env_var(name: str, value: str) -> Path:
    env_name = str(name or "").strip()
    if not is_valid_env_name(env_name):
        raise ValueError(f"非法环境变量名：{env_name}")
    env_value = str(value or "").strip()
    if not env_value:
        raise ValueError("API Key 不能为空")

    existing = read_local_env()
    existing[env_name] = env_value
    LOCAL_ENV_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Local API keys for AI grader. Keep this file private.",
        "# This file is generated/updated by review_app.py.",
    ]
    for key in sorted(existing.keys()):
        lines.append(f"{key}={_encode_env_value(existing[key])}")
    LOCAL_ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return LOCAL_ENV_FILE


def get_local_env_var(name: str) -> str:
    env_name = str(name or "").strip()
    if not env_name:
        return ""
    return read_local_env().get(env_name, "")


def read_local_settings() -> dict:
    if not LOCAL_SETTINGS_FILE.is_file():
        return {}
    try:
        data = json.loads(LOCAL_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def get_export_engine_setting() -> str:
    value = str(read_local_settings().get("export_engine") or "").strip().lower()
    return value if value in EXPORT_ENGINE_VALUES else "katex"


def write_export_engine_setting(engine: str) -> Path:
    normalized = str(engine or "").strip().lower()
    if normalized not in EXPORT_ENGINE_VALUES:
        raise ValueError(f"非法导出引擎：{engine}")
    settings = read_local_settings()
    settings["export_engine"] = normalized
    LOCAL_ENV_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return LOCAL_SETTINGS_FILE


def resolve_api_key(env_name: str) -> tuple[str, str]:
    name = str(env_name or "").strip()
    if not name:
        return "", "missing_env_name"

    from_process = os.getenv(name, "").strip()
    if from_process:
        return from_process, "process_env"

    from_local = get_local_env_var(name).strip()
    if from_local:
        return from_local, "local_env"

    return "", "missing"


def list_assignment_config_paths() -> list[Path]:
    if not ASSIGNMENTS_DIR.is_dir():
        return []
    return sorted(ASSIGNMENTS_DIR.glob("*.json"))


def resolve_relative_path(path_str: str, base_dir: Path) -> Path:
    candidate = Path(path_str)
    if candidate.is_absolute():
        return candidate.resolve()
    return (base_dir / candidate).resolve()


def load_subject_config(subject_config_path: Path | None = None) -> SubjectConfig:
    resolved_path = (subject_config_path or DEFAULT_SUBJECT_CONFIG).resolve()
    data = load_json(resolved_path)
    base_dir = resolved_path.parent
    return SubjectConfig(
        subject_id=data["subject_id"],
        subject_name=data["subject_name"],
        model=data["model"],
        base_url=data["base_url"],
        api_key_env=data["api_key_env"],
        prompt_template_path=resolve_relative_path(data["prompt_template"], base_dir),
        grading_requirements=data["grading_requirements"],
        output_format=data["output_format"],
    )


def load_assignment_config(assignment_path: Path) -> AssignmentConfig:
    resolved_path = assignment_path.resolve()
    data = load_json(resolved_path)
    base_dir = resolved_path.parent
    subject_config_path = data.get("subject_config")
    subject = (
        load_subject_config(resolve_relative_path(subject_config_path, base_dir))
        if subject_config_path
        else load_subject_config()
    )
    return AssignmentConfig(
        assignment_id=data["assignment_id"],
        week_name=data["week_name"],
        week_dir=resolve_relative_path(data["week_dir"], base_dir),
        raw_submissions_dir=resolve_relative_path(data["raw_submissions_dir"], base_dir),
        processed_images_dir=resolve_relative_path(data["processed_images_dir"], base_dir),
        results_dir=resolve_relative_path(data["results_dir"], base_dir),
        answer_key_path=resolve_relative_path(data["answer_key"], base_dir),
        preprocess_summary_path=resolve_relative_path(data["preprocess_summary"], base_dir),
        grading_summary_path=resolve_relative_path(data["grading_summary"], base_dir),
        subject=subject,
    )


def build_default_assignment_config(week: str) -> AssignmentConfig:
    week_dir = (REPO_ROOT / week).resolve()
    subject = load_subject_config()
    answer_key_path = week_dir / DEFAULT_ANSWER_KEY_FILENAME
    if not answer_key_path.exists():
        answer_key_path = week_dir / "Lec1.tex"
    return AssignmentConfig(
        assignment_id=f"default_{week}",
        week_name=week_dir.name,
        week_dir=week_dir,
        raw_submissions_dir=week_dir / "raw_submissions",
        processed_images_dir=week_dir / "processed_images",
        results_dir=week_dir / "results",
        answer_key_path=answer_key_path,
        preprocess_summary_path=week_dir / "preprocess_summary.txt",
        grading_summary_path=week_dir / "summary.txt",
        subject=subject,
    )


def load_runtime_config(assignment: str | None = None, week: str | None = None) -> AssignmentConfig:
    if assignment:
        return load_assignment_config(Path(assignment))
    if week:
        return build_default_assignment_config(week)
    assignment_paths = list_assignment_config_paths()
    if len(assignment_paths) == 1:
        return load_assignment_config(assignment_paths[0])
    if assignment_paths:
        available = ", ".join(path.stem for path in assignment_paths)
        raise ValueError(f"必须提供 --assignment 或 --week。当前可用 assignment: {available}")
    raise ValueError("必须提供 --assignment 或 --week，且当前未发现任何 assignment 配置文件。")
