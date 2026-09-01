from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PREFIXES = (
    ".codex/skills/",
)
FORBIDDEN_FILES = {
    "configs/teacher_labeling.json",
    "configs/env/local.env",
    "docs/langgraph-multi-agent-grading-upgrade-plan.md",
    "docs/layout-dataset-cloud-readiness.md",
}
SECRET_PATTERNS = {
    "OpenAI key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "GitHub token": re.compile(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}"),
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "Aliyun access key": re.compile(r"LTAI[A-Za-z0-9]{12,}"),
    "private key": re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    "long bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{24,}"),
    "student id and name": re.compile(r"\d{11,12}[-_][\u4e00-\u9fff]{2,}"),
}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main() -> int:
    findings: list[str] = []
    paths = tracked_files()
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized in FORBIDDEN_FILES or any(
            normalized.startswith(prefix) for prefix in FORBIDDEN_PREFIXES
        ):
            findings.append(f"forbidden tracked path: {normalized}")
            continue

        data = (ROOT / path).read_bytes()
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="ignore")
        for label, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{label}: {normalized}:{line}")

    if findings:
        print("Public release check failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print(f"Public release check passed for {len(paths)} tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
