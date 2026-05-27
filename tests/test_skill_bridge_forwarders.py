"""Tests for packaged skill bridge launcher scripts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agy_mcp import __version__ as _AGY_MCP_VERSION
from agy_mcp.install import _VERSION_PLACEHOLDER, _template_skill_body

# Resolve relative to THIS test file rather than pytest's cwd; otherwise
# running ``pytest tests/test_skill_bridge_forwarders.py`` from a sibling
# directory fails with ``FileNotFoundError`` (Phase 8 review P1.1).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGED_SKILL_ROOT = _REPO_ROOT / "src" / "agy_mcp" / "_skill_bodies"


def _forwarder_path(target: str) -> Path:
    return _PACKAGED_SKILL_ROOT / target / "scripts" / "agy_bridge.py"


def _load_forwarder(target: str):
    """Load the packaged forwarder script and return its module object."""

    script = _forwarder_path(target)
    spec = importlib.util.spec_from_file_location(
        f"agy_bridge_forwarder_{target}",
        script,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_skill_forwarder_uvx_spec_prefers_installed_version(
    monkeypatch, target: str
):
    """When agy_mcp is importable (always, in tests), the forwarder pins
    the running installation's exact version — so a wheel upgrade is
    picked up by the uvx fallback without re-installing the skill."""

    module = _load_forwarder(target)

    monkeypatch.delenv("AGY_BRIDGE_CMD", raising=False)
    monkeypatch.setattr(module, "_has_module", lambda name: False)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fake/uvx")

    command = module._select_command(["--PROMPT", "hello"])
    assert command == [
        "uvx",
        "--from",
        f"agy-mcp=={_AGY_MCP_VERSION}",
        "agy-bridge",
        "--PROMPT",
        "hello",
    ]
    assert "@main" not in " ".join(command)
    assert _VERSION_PLACEHOLDER not in " ".join(command)


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_skill_forwarder_carries_static_fallback_placeholder(target: str):
    """The packaged source must carry the install-time placeholder
    verbatim, so ``agy_mcp.install._template_skill_body`` has something
    to substitute when a wheel-less host installs the bundle."""

    raw = _forwarder_path(target).read_text(encoding="utf-8")
    assert f'"agy-mcp=={_VERSION_PLACEHOLDER}"' in raw, (
        f"forwarder for {target} must keep the templating placeholder so "
        f"install.py can substitute it; got:\n{raw}"
    )


def test_template_skill_body_resolves_placeholder():
    """Sanity-check the install-time substitution routine itself."""

    body = (
        '"""docstring"""\n'
        f'BRIDGE_PACKAGE_SPEC = "agy-mcp=={_VERSION_PLACEHOLDER}"\n'
    )
    templated = _template_skill_body(body)
    assert _VERSION_PLACEHOLDER not in templated
    assert f'"agy-mcp=={_AGY_MCP_VERSION}"' in templated


def test_codex_forwarder_uses_codex_specific_copy():
    raw = _forwarder_path("codex").read_text(encoding="utf-8")

    assert "Codex skill" in raw
    assert "Codex agent" in raw
    assert "Claude skill" not in raw
    assert "Claude agent" not in raw
