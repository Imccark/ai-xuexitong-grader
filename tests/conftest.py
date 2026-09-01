from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("online smoke")
    group.addoption(
        "--online",
        action="store_true",
        default=False,
        help="Enable the explicitly opt-in provider smoke test.",
    )
    group.addoption(
        "--max-calls",
        type=int,
        default=0,
        help="Maximum number of online provider calls.",
    )
    group.addoption(
        "--max-input-tokens",
        type=int,
        default=0,
        help="Maximum input-token budget for online smoke tests.",
    )
    group.addoption(
        "--max-output-tokens",
        type=int,
        default=0,
        help="Maximum output-token budget for online smoke tests.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "online: requires explicit online provider access")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--online"):
        if config.getoption("--max-calls") < 3:
            raise pytest.UsageError("--online requires --max-calls >= 3")
        if config.getoption("--max-input-tokens") <= 0:
            raise pytest.UsageError("--online requires --max-input-tokens > 0")
        if config.getoption("--max-output-tokens") <= 0:
            raise pytest.UsageError("--online requires --max-output-tokens > 0")
        return

    skip_online = pytest.mark.skip(reason="online smoke disabled; pass --online with budgets")
    for item in items:
        if item.get_closest_marker("online"):
            item.add_marker(skip_online)


@pytest.fixture
def workspace_tmp_path() -> Path:
    root = Path.cwd() / "temp" / f".phase1-test-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
