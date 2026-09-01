from __future__ import annotations

import argparse
import json
from pathlib import Path

from grading_graph.reference_compiler import REFERENCE_COMPILER_VERSION, compile_reference_answer
from grading_graph.store import atomic_write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="编译当前项目全部周次标准答案并生成题目 manifest。")
    parser.add_argument("--root", default=".", help="项目根目录")
    parser.add_argument("--output", required=True, help="manifest/cache 输出目录")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    output = Path(args.output).resolve()
    answer_paths = sorted(path for path in root.glob("第*周/answer.tex") if path.is_file())
    manifests = []
    for answer_path in answer_paths:
        week = answer_path.parent.name
        manifest = compile_reference_answer(
            answer_path,
            cache_dir=output / week,
            assignment_id=week,
        )
        atomic_write_json(output / week / "manifest.json", manifest.model_dump(mode="json"))
        source_characters = len(answer_path.read_text(encoding="utf-8"))
        average_slice_ratio = (
            sum(item.character_count for item in manifest.questions.values())
            / len(manifest.questions)
            / source_characters
            if manifest.questions and source_characters
            else 0.0
        )
        manifests.append(
            {
                "assignment_id": week,
                "status": manifest.reference_status,
                "question_count": len(manifest.questions),
                "alias_count": sum(len(item.aliases) for item in manifest.questions.values()),
                "average_slice_ratio": round(average_slice_ratio, 6),
            }
        )
    summary = {
        "schema_version": "1.0",
        "compiler_version": REFERENCE_COMPILER_VERSION,
        "week_count": len(manifests),
        "ready_count": sum(1 for item in manifests if item["status"] == "ready"),
        "max_average_slice_ratio": max((item["average_slice_ratio"] for item in manifests), default=0.0),
        "manifests": manifests,
        "teacher_confirmation": "pending",
    }
    atomic_write_json(output / "summary.json", summary)
    print(json.dumps({key: summary[key] for key in ("week_count", "ready_count")}, ensure_ascii=False))
    return 0 if summary["ready_count"] == summary["week_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
