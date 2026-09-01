from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import mimetypes
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.project_config import REPO_ROOT, load_json, resolve_api_key


DEFAULT_CONFIG = REPO_ROOT / "app" / "configs" / "teacher_labeling.json"
DEFAULT_OUTPUT = REPO_ROOT / "runtime_logs" / "teacher_labeling" / "api_concurrency_benchmark.json"


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


async def _one_request(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    api_key: str,
    model: str,
    request_id: str,
    image_data_url: str | None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        if image_data_url:
            from tools.evaluation.core.layout_teacher import LAYOUT_SCHEMA, PASS_PROMPTS

            content: Any = [
                {"type": "text", "text": PASS_PROMPTS["proposal"]},
                {"type": "image_url", "image_url": {"url": image_data_url, "detail": "original"}},
            ]
            response_format: dict[str, Any] | None = {
                "type": "json_schema",
                "json_schema": {"name": "homework_page_layout", "strict": True, "schema": LAYOUT_SCHEMA},
            }
            max_tokens = 4000
        else:
            content = f"Concurrency probe {request_id}. Reply with exactly OK."
            response_format = None
            max_tokens = 64
        request_body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "reasoning_effort": "low",
            "verbosity": "low",
            "max_tokens": max_tokens,
        }
        if response_format:
            request_body["response_format"] = response_format
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json=request_body,
        )
        elapsed = time.perf_counter() - started
        payload: dict[str, Any] = {}
        if response.status_code == 200:
            try:
                decoded = response.json()
                payload = decoded if isinstance(decoded, dict) else {}
            except ValueError:
                pass
        choices = payload.get("choices") or []
        reported_model = str(payload.get("model") or "")
        return {
            "ok": response.status_code == 200 and bool(choices),
            "status_code": response.status_code,
            "latency_seconds": round(elapsed, 3),
            "reported_model": reported_model,
            "error_type": "" if response.status_code == 200 else f"http_{response.status_code}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": None,
            "latency_seconds": round(time.perf_counter() - started, 3),
            "reported_model": "",
            "error_type": type(exc).__name__,
        }


async def benchmark(
    *,
    endpoint: str,
    api_key: str,
    model: str,
    levels: list[int],
    rounds: int,
    timeout_seconds: float,
    image_data_url: str | None,
) -> list[dict[str, Any]]:
    limits = httpx.Limits(max_connections=max(levels), max_keepalive_connections=max(levels))
    timeout = httpx.Timeout(timeout_seconds)
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=timeout, limits=limits, http2=False) as client:
        for concurrency in levels:
            samples: list[dict[str, Any]] = []
            wall_times: list[float] = []
            for round_index in range(rounds):
                wave_started = time.perf_counter()
                wave = await asyncio.gather(
                    *[
                        _one_request(
                            client,
                            endpoint=endpoint,
                            api_key=api_key,
                            model=model,
                            request_id=f"c{concurrency}-r{round_index}-n{request_index}",
                            image_data_url=image_data_url,
                        )
                        for request_index in range(concurrency)
                    ]
                )
                wall_times.append(time.perf_counter() - wave_started)
                samples.extend(wave)
                await asyncio.sleep(0.5)
            latencies = [float(item["latency_seconds"]) for item in samples]
            successes = [item for item in samples if item["ok"]]
            status_counts: dict[str, int] = {}
            error_counts: dict[str, int] = {}
            for item in samples:
                status = str(item["status_code"])
                status_counts[status] = status_counts.get(status, 0) + 1
                error = str(item["error_type"])
                if error:
                    error_counts[error] = error_counts.get(error, 0) + 1
            results.append(
                {
                    "concurrency": concurrency,
                    "rounds": rounds,
                    "requests": len(samples),
                    "successes": len(successes),
                    "failures": len(samples) - len(successes),
                    "success_rate": round(len(successes) / len(samples), 6),
                    "median_latency_seconds": round(statistics.median(latencies), 3),
                    "p95_latency_seconds": _percentile(latencies, 0.95),
                    "max_latency_seconds": round(max(latencies), 3),
                    "mean_wave_seconds": round(statistics.mean(wall_times), 3),
                    "status_counts": status_counts,
                    "error_counts": error_counts,
                    "reported_models": sorted({item["reported_model"] for item in successes}),
                }
            )
            if len(successes) != len(samples):
                break
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the annotation relay with tiny text-only requests.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--levels", nargs="+", type=int, default=[1, 2, 4, 8, 12, 16, 24, 32])
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--image", help="Optional already-authorized image for a production-shaped multimodal probe.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    if args.rounds < 1 or not args.levels or any(level < 1 for level in args.levels):
        raise SystemExit("levels and rounds must be positive")
    config = load_json(Path(args.config).resolve())
    provider = config["provider"]
    api_key, key_source = resolve_api_key(str(provider["api_key_env"]))
    if not api_key:
        raise SystemExit(f"missing API key: {provider['api_key_env']}")
    endpoint = str(provider["base_url"]).rstrip("/") + "/" + str(provider["endpoint"]).lstrip("/")
    model = str(provider["model"])
    levels = sorted(set(args.levels))
    image_data_url = None
    if args.image:
        image_path = Path(args.image).resolve()
        mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
        image_data_url = f"data:{mime_type};base64,{base64.b64encode(image_path.read_bytes()).decode('ascii')}"
    started = time.perf_counter()
    levels_report = asyncio.run(
        benchmark(
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            levels=levels,
            rounds=args.rounds,
            timeout_seconds=args.timeout_seconds,
            image_data_url=image_data_url,
        )
    )
    all_pass_levels = [item["concurrency"] for item in levels_report if item["failures"] == 0]
    first_failure = next((item["concurrency"] for item in levels_report if item["failures"]), None)
    report = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "multimodal_annotation_api_concurrency_probe" if image_data_url else "text_only_annotation_api_concurrency_probe",
        "contains_student_data": bool(image_data_url),
        "payload_mode": "multimodal_layout_proposal" if image_data_url else "tiny_text",
        "provider": str(provider["name"]),
        "request_model": model,
        "key_source": key_source,
        "levels_requested": levels,
        "rounds": args.rounds,
        "total_elapsed_seconds": round(time.perf_counter() - started, 3),
        "highest_all_success_concurrency": max(all_pass_levels) if all_pass_levels else 0,
        "first_observed_failure_concurrency": first_failure,
        "levels": levels_report,
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
