from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from app.grading_graph.nodes.local_layout import (
    LocalLayoutObserver,
    LocalLayoutSettings,
    LocalLayoutUnavailable,
)
from app.grading_graph.schemas import AnswerManifest
from app.grading_graph.store import atomic_write_json


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "app" / "configs" / "agent_pipeline.json"


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="离线测试本机 PP-DocLayoutV3 切题性能和门禁通过率。")
    value.add_argument("--images", required=True, help="包含 PNG/JPG 页面的目录")
    value.add_argument("--answer-manifest", required=True, help="已编译的 answer manifest JSON")
    value.add_argument("--config", default=str(DEFAULT_CONFIG), help="Agent pipeline 配置")
    value.add_argument("--max-pages", type=int, default=200)
    value.add_argument("--warmup-pages", type=int, default=3)
    value.add_argument("--output", default=str(ROOT / "temp" / "local-layout-benchmark.json"))
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def benchmark(
    *,
    image_dir: Path,
    manifest_path: Path,
    config_path: Path,
    max_pages: int,
    warmup_pages: int,
) -> dict[str, Any]:
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    settings = LocalLayoutSettings.from_mapping(config.get("local_layout"), base_dir=ROOT)
    if not settings.enabled:
        raise LocalLayoutUnavailable("local layout is disabled in configuration")
    manifest = AnswerManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    observer = LocalLayoutObserver(settings, manifest)
    paths = sorted(
        path
        for path in image_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    )[:max_pages]
    if not paths:
        raise FileNotFoundError(f"no benchmark images found: {image_dir}")

    rows: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        started = time.perf_counter()
        try:
            result = observer.observe(path, page=index + 1)
            elapsed_ms = (time.perf_counter() - started) * 1000
            audit = dict(result.get("audit") or {})
            rows.append(
                {
                    "image_sha256": _sha256(path),
                    "warmup": index < warmup_pages,
                    "status": audit.get("status", "unknown"),
                    "elapsed_ms": round(elapsed_ms, 3),
                    "question_count": len(result.get("observation", {}).get("questions", [])),
                    "region_count": len(audit.get("regions", [])),
                    "reasons": list(audit.get("reasons", [])),
                }
            )
        except LocalLayoutUnavailable:
            raise
        except Exception as exc:
            rows.append(
                {
                    "image_sha256": _sha256(path),
                    "warmup": index < warmup_pages,
                    "status": "failed",
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_type": type(exc).__name__,
                }
            )

    measured = [row for row in rows if not row["warmup"]]
    latencies = [float(row["elapsed_ms"]) for row in measured if row["status"] != "failed"]
    accepted = sum(row["status"] == "accepted" for row in measured)
    rejected = sum(row["status"] == "rejected" for row in measured)
    failed = sum(row["status"] == "failed" for row in measured)
    return {
        "schema_version": "1.0",
        "model": settings.audit_dict(),
        "manifest_hash": _sha256(manifest_path),
        "pages_total": len(rows),
        "pages_measured": len(measured),
        "accepted": accepted,
        "rejected": rejected,
        "failed": failed,
        "acceptance_rate": round(accepted / len(measured), 6) if measured else 0.0,
        "latency_ms": {
            "p50": round(_percentile(latencies, 0.50), 3),
            "p95": round(_percentile(latencies, 0.95), 3),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "pages": rows,
    }


def main() -> int:
    args = parser().parse_args()
    try:
        report = benchmark(
            image_dir=Path(args.images).resolve(),
            manifest_path=Path(args.answer_manifest).resolve(),
            config_path=Path(args.config).resolve(),
            max_pages=args.max_pages,
            warmup_pages=max(0, args.warmup_pages),
        )
    except LocalLayoutUnavailable as exc:
        print(json.dumps({"status": "unavailable", "error_type": type(exc).__name__}, ensure_ascii=False))
        return 2
    output = Path(args.output).resolve()
    atomic_write_json(output, report)
    print(json.dumps({"status": "completed", "output": str(output), **{key: report[key] for key in ("pages_measured", "accepted", "rejected", "failed", "latency_ms")}}, ensure_ascii=False))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
