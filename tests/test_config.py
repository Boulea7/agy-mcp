"""Tests for agy_mcp.config — TOML loading, env-var overrides, precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from agy_mcp.config import (
    DEFAULT_ALLOW_WRITE,
    DEFAULT_BACKEND,
    DEFAULT_OUTPUT_PROTOCOL,
    DEFAULT_WORKTREE,
    default_config_path,
    default_session_store_root,
    load_config,
)


def test_defaults_match_user_decision(isolated_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Point HOME away so default_config_path can't find a real user file.
    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config(path=tmp_path / "missing.toml")
    assert config.execute.worktree_default is DEFAULT_WORKTREE is True
    assert config.execute.allow_write_default is DEFAULT_ALLOW_WRITE is False
    assert config.backend.prefer == DEFAULT_BACKEND == "auto"
    assert config.backend.output_protocol == DEFAULT_OUTPUT_PROTOCOL == "claude"
    assert config.session_store.root  # filled with default path


def test_toml_overrides_defaults(isolated_env, tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[execute]
worktree_default = false
allow_write_default = true

[backend]
prefer = "agy"
output_protocol = "raw"
agy_bin = "/opt/agy/bin/agy"

[safety]
denylist_extra = ["TOPSECRET-PROJECT-CODE"]
scrub_extra_env = ["MY_INTERNAL_TOKEN"]

[session_store]
root = "/tmp/agy-sessions"
retention_days = 7
""".strip(),
        encoding="utf-8",
    )
    config = load_config(path=cfg)
    assert config.execute.worktree_default is False
    assert config.execute.allow_write_default is True
    assert config.backend.prefer == "agy"
    assert config.backend.output_protocol == "raw"
    assert config.backend.agy_bin == "/opt/agy/bin/agy"
    assert config.safety.denylist_extra == ["TOPSECRET-PROJECT-CODE"]
    assert config.safety.scrub_extra_env == ["MY_INTERNAL_TOKEN"]
    assert config.session_store.root == "/tmp/agy-sessions"
    assert config.session_store.retention_days == 7
    assert config.source.endswith("config.toml")


def test_env_overrides_toml(isolated_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[execute]
worktree_default = true

[backend]
prefer = "auto"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGY_MCP_WORKTREE_DEFAULT", "0")
    monkeypatch.setenv("AGY_MCP_BACKEND", "gemini")
    monkeypatch.setenv("AGY_BIN", "/opt/agy/agy")
    config = load_config(path=cfg)
    assert config.execute.worktree_default is False
    assert config.backend.prefer == "gemini"
    assert config.backend.agy_bin == "/opt/agy/agy"


def test_malformed_toml_falls_back_to_defaults(isolated_env, tmp_path: Path):
    bad = tmp_path / "config.toml"
    bad.write_text("not a valid = toml [[[", encoding="utf-8")
    config = load_config(path=bad)
    assert config.execute.worktree_default is True  # default still applies
    assert "failed to read" in config.source


def test_default_session_store_root_uses_home(isolated_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = default_session_store_root()
    assert str(root).startswith(str(tmp_path.resolve()))
    assert root.name == "sessions"


def test_default_config_path_honours_xdg(isolated_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = default_config_path()
    assert path.parent.name == "agy-mcp"
    assert path.parent.parent == tmp_path
