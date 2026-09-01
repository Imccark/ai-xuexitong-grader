from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderCall:
    mode: str
    input_tokens: int
    output_tokens: int


def _estimate_tokens(value: Any) -> int:
    if isinstance(value, bytes):
        return max(1, (len(value) + 3) // 4)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


class FakeProvider:
    """Deterministic provider used by offline tests and future graph nodes."""

    def __init__(self) -> None:
        self.calls: list[ProviderCall] = []

    def complete_text(self, prompt: str) -> str:
        self.calls.append(ProviderCall("text", _estimate_tokens(prompt), 1))
        return "fake-text-response"

    def complete_image(self, prompt: str, image_bytes: bytes) -> str:
        self.calls.append(
            ProviderCall("image", _estimate_tokens(prompt) + _estimate_tokens(image_bytes), 1)
        )
        return "fake-image-response"

    def complete_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(ProviderCall("json", _estimate_tokens(prompt) + _estimate_tokens(schema), 4))
        return {"ok": True, "provider": "fake"}


class OpenAICompatSmokeProvider:
    """Small, budgeted adapter for the explicit online smoke test only.

    It deliberately exposes only three tiny operations and never logs or returns
    the API key. Student data is not accepted by this adapter.
    """

    def __init__(self, client: Any, model: str, budget: dict[str, int]) -> None:
        self.client = client
        self.model = model
        self.budget = budget
        self.calls: list[ProviderCall] = []

    @classmethod
    def from_environment(cls, budget: dict[str, int]) -> "OpenAICompatSmokeProvider":
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not configured")
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        return cls(client, "qwen3.7-plus", budget)

    def _before_call(self, mode: str, input_tokens: int) -> None:
        if len(self.calls) >= self.budget["max_calls"]:
            raise RuntimeError("online smoke call budget exhausted")
        used_input = sum(call.input_tokens for call in self.calls)
        if used_input + input_tokens > self.budget["max_input_tokens"]:
            raise RuntimeError("online smoke input-token budget exhausted")

    def _record(self, mode: str, response: Any, estimated_input: int) -> None:
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None) or estimated_input
        output_tokens = getattr(usage, "completion_tokens", None) or 0
        if sum(call.output_tokens for call in self.calls) + output_tokens > self.budget["max_output_tokens"]:
            raise RuntimeError("online smoke output-token budget exhausted")
        self.calls.append(ProviderCall(mode, int(input_tokens), int(output_tokens)))

    @staticmethod
    def _content(response: Any) -> str:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("provider returned no choices")
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if not content:
            raise RuntimeError("provider returned empty content")
        return str(content)

    def complete_text(self, prompt: str) -> str:
        estimated = _estimate_tokens(prompt)
        self._before_call("text", estimated)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"enable_thinking": False},
            max_tokens=32,
        )
        self._record("text", response, estimated)
        return self._content(response)

    def complete_image(self, prompt: str, image_bytes: bytes) -> str:
        # The smoke test always uses a generated 16x16 PNG; the caller's bytes
        # are counted for the budget but are not sent as potentially invalid
        # content. DashScope vision models reject images whose width/height is
        # <=10, so a 1x1 fixture would test the wrong failure mode.
        tiny_png = base64.b64encode(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000100000001008060000001ff3ff6100"
                "00001649444154789c63f84f2160183560d4805103868b01005d78fc2eaf"
                "0000000049454e44ae426082"
            )
        ).decode("ascii")
        estimated = _estimate_tokens(prompt) + _estimate_tokens(image_bytes)
        self._before_call("image", estimated)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tiny_png}"}},
                    ],
                }
            ],
            extra_body={"enable_thinking": False},
            max_tokens=32,
        )
        self._record("image", response, estimated)
        return self._content(response)

    def complete_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        estimated = _estimate_tokens(prompt) + _estimate_tokens(schema)
        self._before_call("json", estimated)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
            max_tokens=64,
        )
        self._record("json", response, estimated)
        value = json.loads(self._content(response))
        if not isinstance(value, dict):
            raise RuntimeError("provider structured response was not an object")
        return value
