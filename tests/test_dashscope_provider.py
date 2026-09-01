from __future__ import annotations

from types import SimpleNamespace

from app.grading_graph.provider import (
    DASHSCOPE_BASE_URL,
    DASHSCOPE_MODEL,
    DashScopeOpenAIProvider,
    _dead_loopback_proxy_sentinel,
)
from app.grading_graph.cache import CachedJsonProvider, JsonResponseCache
from app.grading_graph.budget import BudgetLedger, BudgetedJsonProvider


class _FakeCompletions:
    def __init__(self, content: str = '{"ok": true}') -> None:
        self.calls = []
        self.content = content

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
        )


def test_dashscope_provider_uses_locked_openai_compatible_contract(workspace_tmp_path) -> None:
    completions = _FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = DashScopeOpenAIProvider(client)
    image_path = workspace_tmp_path / "page.png"
    image_path.write_bytes(b"fake-image")

    assert provider.complete_json("return json", {"type": "object"}) == {"ok": True}
    assert provider.complete_json("inspect image", {"type": "object"}, image_ref=str(image_path)) == {"ok": True}
    assert completions.calls[0]["model"] == DASHSCOPE_MODEL
    assert completions.calls[0]["extra_body"] == {"enable_thinking": False}
    assert completions.calls[1]["messages"][0]["content"][1]["type"] == "image_url"
    assert provider.usage.calls == 2
    assert provider.usage.input_tokens == 14
    assert provider.usage.output_tokens == 6
    assert DASHSCOPE_BASE_URL.endswith("/compatible-mode/v1")


def test_only_dead_localhost_port_9_proxy_is_treated_as_sentinel() -> None:
    assert _dead_loopback_proxy_sentinel(
        {
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://localhost:9",
            "ALL_PROXY": "http://[::1]:9",
        }
    ) is True
    assert _dead_loopback_proxy_sentinel({"HTTPS_PROXY": "http://127.0.0.1:7890"}) is False
    assert _dead_loopback_proxy_sentinel({"HTTPS_PROXY": "http://proxy.example:8080"}) is False
    assert _dead_loopback_proxy_sentinel({}) is False


def test_dashscope_provider_wraps_qwen_transcriber_array() -> None:
    completions = _FakeCompletions('[{"text":"-1","confidence":0.9}]')
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = DashScopeOpenAIProvider(client)

    schema = {"type": "object", "properties": {"spans": {"type": "array"}}}
    assert provider.complete_json("return JSON", schema) == {
        "spans": [{"text": "-1", "confidence": 0.9}]
    }


def test_dashscope_provider_wraps_qwen_transcriber_lines() -> None:
    completions = _FakeCompletions('{"lines":[{"page":3,"bbox":[0,0,10,10],"text":"-1","confidence":0.9}]}')
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = DashScopeOpenAIProvider(client)
    schema = {"type": "object", "properties": {"spans": {"type": "array"}}}
    assert provider.complete_json("return JSON", schema)["spans"][0]["text"] == "-1"


def test_dashscope_provider_unwraps_nested_transcriber_content() -> None:
    completions = _FakeCompletions('{"content":"{\\"spans\\": [{\\"text\\": \\"x\\"}]}"}')
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = DashScopeOpenAIProvider(client)
    schema = {"type": "object", "properties": {"spans": {"type": "array"}}}
    assert provider.complete_json("return JSON", schema)["spans"][0]["text"] == "x"


def test_dashscope_provider_wraps_symbol_audit_array() -> None:
    completions = _FakeCompletions('[{"symbol":"minus","confidence":0.9}]')
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = DashScopeOpenAIProvider(client)
    schema = {"type": "object", "properties": {"symbol_candidates": {"type": "array"}}}
    assert provider.complete_json("return JSON", schema)["symbol_candidates"][0]["symbol"] == "minus"


def test_dashscope_provider_accepts_fenced_json_object() -> None:
    completions = _FakeCompletions('```json\n{"symbol_candidates": [], "decisive": false, "reason": "uncertain"}\n```')
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = DashScopeOpenAIProvider(client)
    schema = {"type": "object", "properties": {"symbol_candidates": {"type": "array"}}}
    assert provider.complete_json("return JSON", schema)["decisive"] is False


def test_cached_provider_normalizes_legacy_transcriber_lines(workspace_tmp_path) -> None:
    cache = JsonResponseCache(workspace_tmp_path / "cache")
    schema = {"type": "object", "properties": {"spans": {"type": "array"}}}
    key = cache.key(prompt="p", schema=schema, model="m", preprocess_version=cache.preprocess_version)
    cache.put(key, {"lines": [{"text": "x"}]})
    provider = CachedJsonProvider(SimpleNamespace(model="m"), cache)
    result = provider.complete_json("p", schema)
    assert result == {"spans": [{"text": "x"}]}


def test_cache_hit_bypasses_paid_call_budget(workspace_tmp_path) -> None:
    class Provider:
        model = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def complete_json(self, prompt, schema, image_ref=None):
            self.calls += 1
            return {"ok": True}

    base = Provider()
    ledger = BudgetLedger(
        {"max_calls": 1, "max_input_tokens": 1000, "max_output_tokens": 1000}
    )
    paid = BudgetedJsonProvider(base, ledger)
    cached = CachedJsonProvider(paid, JsonResponseCache(workspace_tmp_path / "cache"))
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

    assert cached.complete_json("same", schema) == {"ok": True}
    assert cached.complete_json("same", schema) == {"ok": True}
    assert base.calls == 1
    assert ledger.snapshot.calls == 1
