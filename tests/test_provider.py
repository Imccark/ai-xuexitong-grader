from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.evaluation.core.provider import FakeProvider


def test_fake_provider_covers_text_image_and_structured_json() -> None:
    provider = FakeProvider()

    text = provider.complete_text("offline prompt")
    image = provider.complete_image("offline image prompt", b"fake-image")
    structured = provider.complete_json(
        "offline json prompt",
        {"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )

    assert text == "fake-text-response"
    assert image == "fake-image-response"
    assert structured == {"ok": True, "provider": "fake"}
    assert [call.mode for call in provider.calls] == ["text", "image", "json"]
    assert all(call.input_tokens >= 0 for call in provider.calls)
    assert all(call.output_tokens >= 0 for call in provider.calls)


def test_offline_provider_fixture_is_data_only() -> None:
    fixture_path = Path(__file__).parents[1] / "tools" / "evaluation" / "core" / "fixtures" / "provider_responses.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture == {
        "text": "fake-text-response",
        "image": "fake-image-response",
        "json": {"ok": True, "provider": "fake"},
    }


@pytest.mark.online
def test_online_provider_smoke(online_budget: dict[str, int]) -> None:
    from tools.evaluation.core.provider import OpenAICompatSmokeProvider

    provider = OpenAICompatSmokeProvider.from_environment(online_budget)
    assert provider.complete_text("Reply with the word ok.")
    assert provider.complete_image("Describe this 16x16 image in one word.", b"tiny-png")
    assert provider.complete_json(
        "Return JSON with ok=true.",
        {"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )["ok"] is True
    assert len(provider.calls) == 3


@pytest.fixture
def online_budget(request: pytest.FixtureRequest) -> dict[str, int]:
    config = request.config
    return {
        "max_calls": config.getoption("--max-calls"),
        "max_input_tokens": config.getoption("--max-input-tokens"),
        "max_output_tokens": config.getoption("--max-output-tokens"),
    }
