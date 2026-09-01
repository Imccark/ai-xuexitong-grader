from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


# DashScope's provider name is not itself a credential.  Keep the detector
# strict enough to reject key-shaped values without rejecting strings such as
# ``dashscope-openai-compatible`` in configuration metadata.
SECRET_VALUE_PATTERN = re.compile(r"(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|dashscope-[A-Za-z0-9]{24,})")
SECRET_FIELD_NAMES = {
    "api_key",
    "apikey",
    "dashscope_api_key",
    "openai_api_key",
    "secret_key",
    "authorization",
    "x_api_key",
    "x-api-key",
}
STUDENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_no_secret(value: Any, key: str = "") -> None:
    if isinstance(value, bytes):
        raise ValueError("artifact cannot contain bytes; store a file reference instead")
    if isinstance(value, str):
        if SECRET_VALUE_PATTERN.search(value):
            raise ValueError("secret-like value cannot be written to artifact")
        if key.lower() in SECRET_FIELD_NAMES:
            raise ValueError("API key fields cannot be written to artifact")
    elif isinstance(value, dict):
        for item_key, item_value in value.items():
            _validate_no_secret(item_value, str(item_key))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _validate_no_secret(item, key)


def canonical_json(value: Any) -> str:
    _validate_no_secret(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-", suffix=path.suffix or ".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return path


def atomic_write_json(path: Path, value: Any) -> Path:
    _validate_no_secret(value)
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return atomic_write_bytes(path, data)


class ArtifactStore:
    """Filesystem artifact store with atomic JSON writes and strict references."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_hash(student_hash: str) -> str:
        if not STUDENT_HASH_PATTERN.fullmatch(student_hash):
            raise ValueError("student_hash must be a 64-character lowercase SHA-256")
        return student_hash

    @staticmethod
    def _safe_component(value: str) -> str:
        if not value or value in {".", ".."} or Path(value).name != value:
            raise ValueError("artifact path component is invalid")
        return value

    def student_dir(self, assignment_id: str, student_hash: str) -> Path:
        return self.root / self._safe_component(assignment_id) / "agent_artifacts" / self._safe_hash(student_hash)

    def artifact_path(self, assignment_id: str, student_hash: str, filename: str) -> Path:
        safe_name = self._safe_component(filename)
        if not safe_name.endswith(".json"):
            raise ValueError("artifact filename must end with .json")
        return self.student_dir(assignment_id, student_hash) / safe_name

    def write_json(self, assignment_id: str, student_hash: str, filename: str, value: Any) -> Path:
        return atomic_write_json(self.artifact_path(assignment_id, student_hash, filename), value)

    def read_json(self, assignment_id: str, student_hash: str, filename: str) -> Path:
        path = self.artifact_path(assignment_id, student_hash, filename)
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def write_review_decision(self, assignment_id: str, student_hash: str, value: Any) -> Path:
        _validate_no_secret(value)
        path = self.root / self._safe_component(assignment_id) / "review_decisions" / f"{self._safe_hash(student_hash)}.json"
        return atomic_write_json(path, value)

    def write_final_result(self, assignment_id: str, student_hash: str, value: Any) -> Path:
        _validate_no_secret(value)
        path = self.root / self._safe_component(assignment_id) / "results" / f"{self._safe_hash(student_hash)}.json"
        return atomic_write_json(path, value)
