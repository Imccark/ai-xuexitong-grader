from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def estimate_tokens(value: Any) -> int:
    if isinstance(value, bytes):
        return max(1, (len(value) + 3) // 4)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


class BudgetExceededError(RuntimeError):
    pass


@dataclass(frozen=True)
class BudgetSnapshot:
    calls: int
    input_tokens: int
    output_tokens: int


class BudgetLedger:
    """Thread-safe provider budget ledger shared by parallel question branches."""

    def __init__(self, limits: dict[str, Any]) -> None:
        self.limits = {key: int(limits.get(key, 0)) for key in ("max_calls", "max_input_tokens", "max_output_tokens")}
        self._snapshot = BudgetSnapshot(0, 0, 0)
        self._lock = threading.Lock()

    @property
    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return self._snapshot

    def reserve(self, *, input_tokens: int) -> None:
        with self._lock:
            next_value = BudgetSnapshot(
                self._snapshot.calls + 1,
                self._snapshot.input_tokens + max(0, input_tokens),
                self._snapshot.output_tokens,
            )
            if next_value.calls > self.limits["max_calls"]:
                raise BudgetExceededError("model call budget exhausted")
            if next_value.input_tokens > self.limits["max_input_tokens"]:
                raise BudgetExceededError("input-token budget exhausted")
            self._snapshot = next_value

    def record_output(self, output_tokens: int) -> None:
        with self._lock:
            next_total = self._snapshot.output_tokens + max(0, output_tokens)
            if next_total > self.limits["max_output_tokens"]:
                raise BudgetExceededError("output-token budget exhausted")
            self._snapshot = BudgetSnapshot(self._snapshot.calls, self._snapshot.input_tokens, next_total)


class BudgetedJsonProvider:
    def __init__(self, provider: Any, ledger: BudgetLedger) -> None:
        self.provider = provider
        self.ledger = ledger
        self.model = getattr(provider, "model", "")

    def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        image_ref: str | list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        refs = [] if image_ref is None else ([image_ref] if isinstance(image_ref, str) else list(image_ref))
        image_estimate = 0
        for raw_ref in refs:
            path = Path(str(raw_ref))
            if path.is_file():
                # Conservative cross-provider accounting. Actual usage is still
                # recorded by the provider for the final report.
                image_estimate += max(256, (path.stat().st_size + 1023) // 1024)
        self.ledger.reserve(input_tokens=estimate_tokens(prompt) + estimate_tokens(schema) + image_estimate)
        if image_ref is None:
            value = self.provider.complete_json(prompt, schema)
        else:
            value = self.provider.complete_json(prompt, schema, image_ref=image_ref)
        self.ledger.record_output(estimate_tokens(value))
        return value


class RateLimitedJsonProvider:
    """Bound provider concurrency so outer student and inner question work cannot explode."""

    def __init__(self, provider: Any, *, max_concurrency: int = 4) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.provider = provider
        self.model = getattr(provider, "model", "")
        self._slots = threading.BoundedSemaphore(max_concurrency)

    def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        image_ref: str | list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        with self._slots:
            if image_ref is None:
                return self.provider.complete_json(prompt, schema)
            return self.provider.complete_json(prompt, schema, image_ref=image_ref)
