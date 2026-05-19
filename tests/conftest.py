"""Shared fixtures for agy-mcp test suite."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def tmp_session_root(tmp_path: Path) -> Path:
    """Isolated session-store root for tests that exercise the SessionStore."""

    root = tmp_path / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Clear AGY_MCP_* / agy / gemini env vars so config tests are deterministic."""

    cleared = []
    for key in list(os.environ.keys()):
        if key.startswith("AGY_MCP_") or key in {"AGY_BIN", "GEMINI_BIN", "XDG_CONFIG_HOME"}:
            cleared.append(key)
            monkeypatch.delenv(key, raising=False)
    yield {key: "" for key in cleared}
