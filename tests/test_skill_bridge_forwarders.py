"""Tests for packaged skill bridge launcher scripts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_skill_forwarder_uses_fixed_uvx_package_spec(monkeypatch, target: str):
    script = (
        Path("src/agy_mcp/_skill_bodies")
        / target
        / "scripts"
        / "agy_bridge.py"
    )
    spec = importlib.util.spec_from_file_location(
        f"agy_bridge_forwarder_{target}",
        script,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.delenv("AGY_BRIDGE_CMD", raising=False)
    monkeypatch.setattr(module, "_has_module", lambda name: False)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fake/uvx")

    command = module._select_command(["--PROMPT", "hello"])
    assert command == [
        "uvx",
        "--from",
        "agy-mcp==0.1.0",
        "agy-bridge",
        "--PROMPT",
        "hello",
    ]
    assert "@main" not in " ".join(command)
