from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_MODEL = "qwen3.7-plus"
DASHSCOPE_API_KEY_ENV = "DASHSCOPE_API_KEY"
DASHSCOPE_TIMEOUT_SECONDS = 60.0


def _dead_loopback_proxy_sentinel(environ: dict[str, str] | None = None) -> bool:
    """Detect the non-listening proxy sentinel used by restricted shells.

    Some desktop/sandbox launches set every proxy variable to localhost:9.
    HTTP clients honor those process variables even while the browser and
    WinHTTP use a healthy direct/TUN connection, producing WinError 10061
    before a request reaches DashScope.  Ignore only this exact sentinel; a
    real local or corporate proxy must remain untouched.
    """

    source = os.environ if environ is None else environ
    values = [
        str(source.get(name, "") or "").strip()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
        if str(source.get(name, "") or "").strip()
    ]
    if not values:
        return False
    for raw in values:
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        try:
            port = parsed.port
        except ValueError:
            return False
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or port != 9:
            return False
    return True


@dataclass(frozen=True)
class ProviderUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def _normalized_image_refs(image_ref: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if image_ref is None:
        return []
    if isinstance(image_ref, str):
        return [image_ref]
    return [str(value) for value in image_ref if str(value)]


def _parse_json_object(content: str) -> Any:
    """Accept structured JSON even when a compatible model adds code fences."""

    raw = str(content).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        fenced = re.search(r"```(?:json)?\s*([\[{].*[\]}])\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
        candidate = fenced.group(1) if fenced else raw[raw.find("{") : raw.rfind("}") + 1]
        if not candidate.startswith("{") or not candidate.endswith("}"):
            raise
        value = json.loads(candidate)
    return value


class DashScopeOpenAIProvider:
    """OpenAI-compatible DashScope adapter with no credential logging."""

    def __init__(self, client: Any, *, model: str = DASHSCOPE_MODEL, max_output_tokens: int = 2048) -> None:
        self.client = client
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.usage = ProviderUsage()
        # LangGraph fans out question nodes concurrently. DashScope applies
        # per-key rate limits, so serialize network calls while leaving the
        # graph's deterministic preprocessing and cache lookups parallel.
        self._request_lock = threading.Lock()

    @classmethod
    def from_environment(cls, *, max_output_tokens: int = 2048) -> "DashScopeOpenAIProvider":
        api_key = os.environ.get(DASHSCOPE_API_KEY_ENV, "").strip()
        if not api_key:
            raise RuntimeError(f"{DASHSCOPE_API_KEY_ENV} is not configured")
        from openai import DefaultHttpxClient, OpenAI

        try:
            timeout = max(5.0, float(os.environ.get("DASHSCOPE_TIMEOUT_SECONDS", DASHSCOPE_TIMEOUT_SECONDS)))
        except (TypeError, ValueError):
            timeout = DASHSCOPE_TIMEOUT_SECONDS

        client_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": DASHSCOPE_BASE_URL,
            "timeout": timeout,
            "max_retries": 0,
        }
        if _dead_loopback_proxy_sentinel():
            client_kwargs["http_client"] = DefaultHttpxClient(trust_env=False)
        return cls(
            OpenAI(**client_kwargs),
            model=DASHSCOPE_MODEL,
            max_output_tokens=max_output_tokens,
        )

    @staticmethod
    def _image_content(image_ref: str) -> dict[str, Any]:
        path = Path(image_ref).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        media_type = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{encoded}"}}

    @staticmethod
    def _content(response: Any) -> str:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("provider returned no choices")
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if isinstance(content, list):
            content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        if not content:
            raise RuntimeError("provider returned empty content")
        return str(content)

    def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        image_ref: str | list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        content: Any = prompt
        image_refs = _normalized_image_refs(image_ref)
        if image_refs:
            content = [{"type": "text", "text": prompt}, *(self._image_content(ref) for ref in image_refs)]
        with self._request_lock:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": False},
                max_tokens=self.max_output_tokens,
            )
            usage = getattr(response, "usage", None)
            self.usage = ProviderUsage(
                calls=self.usage.calls + 1,
                input_tokens=self.usage.input_tokens + int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=self.usage.output_tokens + int(getattr(usage, "completion_tokens", 0) or 0),
            )
        value = _parse_json_object(self._content(response))
        properties = schema.get("properties", {}) or {}
        if isinstance(value, dict) and "spans" not in value and "content" in value and "spans" in properties:
            nested = value.get("content")
            if isinstance(nested, str):
                try:
                    nested = json.loads(nested)
                except ValueError:
                    nested = None
            if isinstance(nested, dict):
                value = nested
        # Qwen occasionally returns a transcriber top-level array directly.
        # This schema is the only one where that shape is unambiguous, so keep
        # the compatibility normalization narrowly scoped to ``spans``.
        if isinstance(value, list) and "spans" in properties:
            value = {"spans": value}
        elif isinstance(value, list) and "symbol_candidates" in properties:
            value = {"symbol_candidates": value}
        if isinstance(value, dict) and "spans" not in value and "lines" in value and "spans" in properties:
            value = {"spans": value.get("lines", [])}
        if isinstance(value, dict) and "spans" not in value and "spans" in properties:
            for alias in ("transcription", "transcriptions", "ocr", "results"):
                if alias in value:
                    aliased = value.get(alias, [])
                    if isinstance(aliased, dict):
                        aliased = [aliased]
                    elif isinstance(aliased, str):
                        aliased = [{"text": aliased}]
                    value = {"spans": aliased}
                    break
        if isinstance(value, dict) and "spans" not in value and "span_id" in value and "spans" in properties:
            value = {"spans": [value]}
        if isinstance(value, dict) and "symbol_candidates" not in value and "symbols" in value and "symbol_candidates" in properties:
            value = {**value, "symbol_candidates": value.get("symbols", [])}
        if not isinstance(value, dict):
            raise RuntimeError("provider structured response was not an object")
        return value


class OpenAIResponsesProvider:
    """Independent multimodal judge provider using the OpenAI Responses API.

    Student images are sent only when the caller explicitly runs an online
    evaluation. ``store=False`` prevents creating stored Responses artifacts.
    """

    def __init__(
        self,
        client: Any,
        *,
        model: str = "gpt-5.6",
        max_output_tokens: int = 2048,
        reasoning_effort: str = "high",
    ) -> None:
        self.client = client
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.usage = ProviderUsage()

    @classmethod
    def from_environment(
        cls,
        *,
        model: str = "gpt-5.6",
        api_key_env: str = "OPENAI_API_KEY",
        max_output_tokens: int = 2048,
        reasoning_effort: str = "high",
    ) -> "OpenAIResponsesProvider":
        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(f"{api_key_env} is not configured")
        from openai import OpenAI

        return cls(
            OpenAI(api_key=api_key),
            model=model,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
        )

    @staticmethod
    def _image_content(image_ref: str) -> dict[str, Any]:
        value = DashScopeOpenAIProvider._image_content(image_ref)
        return {
            "type": "input_image",
            "image_url": value["image_url"]["url"],
            "detail": "high",
        }

    def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        image_ref: str | list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        content.extend(self._image_content(ref) for ref in _normalized_image_refs(image_ref))
        response = self.client.responses.create(
            model=self.model,
            input=[{"role": "user", "content": content}],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "multimodal_judge_result",
                    "strict": True,
                    "schema": schema,
                }
            },
            reasoning={"effort": self.reasoning_effort},
            max_output_tokens=self.max_output_tokens,
            store=False,
        )
        usage = getattr(response, "usage", None)
        self.usage = ProviderUsage(
            calls=self.usage.calls + 1,
            input_tokens=self.usage.input_tokens + int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=self.usage.output_tokens + int(getattr(usage, "output_tokens", 0) or 0),
        )
        content_text = str(getattr(response, "output_text", "") or "")
        if not content_text:
            raise RuntimeError("provider returned empty output_text")
        value = json.loads(content_text)
        if not isinstance(value, dict):
            raise RuntimeError("provider structured response was not an object")
        return value
