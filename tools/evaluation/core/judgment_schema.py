from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


VERDICTS = {"correct", "partial", "incorrect", "unreadable", "unknown"}
STUDENT_HASH_RE = re.compile(r"[0-9a-f]{64}")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read an object-only JSONL artifact with useful line errors."""
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}: line {line_number} must be a JSON object")
        records.append(value)
    return records
