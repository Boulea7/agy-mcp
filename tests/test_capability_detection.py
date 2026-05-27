"""Tests for adapter capability probing — pure parser-side coverage.

These tests do not spawn the real binaries; they feed canned ``--help``
output through ``detect_flags`` / ``has_flag`` and the adapter's
internal probe helper so we exercise the pattern surface deterministically.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from agy_mcp.adapters.agy import (
    AGY_AUTH_LOG_LOOKBACK_S,
    AgyPrintBackend,
    _parse_version,
    _parse_version_from_help,
    detect_agy_auth_source,
)
from agy_mcp.adapters.base import detect_flags, has_flag
from agy_mcp.adapters.gemini import GeminiCliBackend

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
FAKE_AGY_PRINT = FIXTURES / "fake_agy_print.py"
FAKE_AGY_WITH_LOG = FIXTURES / "fake_agy_with_log.py"
FAKE_GEMINI = FIXTURES / "fake_gemini_streamjson.py"


# ---------------------------------------------------------------------------
# detect_flags / has_flag
# ---------------------------------------------------------------------------


def test_detect_flags_picks_long_and_short():
    text = """\
  --print         do a thing
  -p, --prompt    alias
  --no-color
  --add-dir DIR
  bare line
"""
    flags = detect_flags(text)
    assert {"--print", "-p", "--prompt", "--no-color", "--add-dir"} <= flags
    # Bare lines without leading hyphen are ignored.
    assert "bare" not in flags


def test_has_flag_matches_any_of_aliases():
    text = "  --print\n  -p"
    assert has_flag(text, "--print")
    assert has_flag(text, "--prompt", "-p")
    assert not has_flag(text, "--nonexistent")


def test_detect_flags_handles_empty_text():
    assert detect_flags("") == set()
    assert not has_flag("", "--print")


# ---------------------------------------------------------------------------
# AgyPrintBackend._probe via fake_agy_print
# ---------------------------------------------------------------------------


def test_agy_probe_against_fake_help(tmp_path, monkeypatch):
    """Probe a fake agy bound via bin_override and verify capability bits."""

    # Wrap the fake script with a small shell stub so the adapter sees a
    # plain executable path (it cannot directly spawn a `.py` file unless
    # we shebang it; we shell-wrap to avoid PATH issues on macOS where the
    # script's shebang line ``#!/usr/bin/env python3`` may pick a slow
    # interpreter).
    wrapper = tmp_path / "fake_agy"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_AGY_PRINT}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    backend = AgyPrintBackend(bin_override=str(wrapper))
    # Avoid touching real ~/.gemini.
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_SETTINGS_PATH", tmp_path / "no-settings.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_GEMINI_SETTINGS_PATH", tmp_path / "no-gemini.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", tmp_path / "no-log-dir")

    cap = backend.detect()
    assert cap.bin_path == str(wrapper)
    assert cap.backend == "agy"
    assert cap.supports_print is True
    assert cap.supports_print_timeout is True
    assert cap.supports_conversation is True
    assert cap.supports_continue is True
    assert cap.supports_sandbox is True
    assert cap.supports_log_file is True
    assert cap.supports_add_dir is True
    assert cap.supports_dangerously_skip_permissions is True
    assert cap.supports_streaming is False
    assert cap.supports_tool_events is False
    # No oauth creds file → must surface auth warning.
    assert cap.authenticated is False
    assert any("auth state not detected" in w for w in cap.warnings)


def test_agy_probe_auth_requires_regular_file(tmp_path, monkeypatch):
    wrapper = tmp_path / "fake_agy"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_AGY_PRINT}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    target = tmp_path / "target-oauth.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "oauth-link.json"
    link.symlink_to(target)

    backend = AgyPrintBackend(bin_override=str(wrapper))
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", link)
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", tmp_path / "no-log-dir")
    cap = backend.detect()
    assert cap.authenticated is False

    real = tmp_path / "oauth-real.json"
    real.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", real)
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", tmp_path / "no-log-dir")
    cap = backend.detect(refresh=True)
    assert cap.authenticated is True


def test_agy_probe_accepts_recent_keyring_auth_log(tmp_path, monkeypatch):
    wrapper = tmp_path / "fake_agy"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_AGY_PRINT}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "cli-20260526_165607.log"
    log_file.write_text(
        "I0526 16:56:08.832422 47678 auth.go:114] ChainedAuth: authenticated via keyring "
        "(effective: keyring)\n"
        "I0526 16:56:08.832561 47678 server_oauth.go:212] applyAuthResult: "
        "email=user@example.com, authMethod=consumer, quotaProject=\n"
        "I0526 16:56:08.832590 47678 server_oauth.go:217] OAuth: authenticated "
        "successfully as user@example.com\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", log_dir)
    backend = AgyPrintBackend(bin_override=str(wrapper))

    cap = backend.detect()
    assert cap.authenticated is True
    auth_source = detect_agy_auth_source()
    assert auth_source is not None
    assert auth_source.kind == "keyring_log"
    assert auth_source.path == log_dir


def test_agy_probe_rejects_keyring_log_after_newer_auth_failure(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "cli-20260526_165607.log").write_text(
        "I0526 16:56:08.832422 47678 auth.go:114] ChainedAuth: authenticated via keyring "
        "(effective: keyring)\n"
        "E0526 16:56:09.000000 47678 print.go:88] Print mode: auth error: token expired\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", log_dir)

    assert detect_agy_auth_source() is None


def test_agy_probe_ignores_stale_keyring_log(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "cli-20260526_165607.log"
    log_file.write_text(
        "I0526 16:56:08.832422 47678 auth.go:114] ChainedAuth: authenticated via keyring "
        "(effective: keyring)\n",
        encoding="utf-8",
    )
    stale = time.time() - AGY_AUTH_LOG_LOOKBACK_S - 60
    os.utime(log_file, (stale, stale))

    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", log_dir)

    assert detect_agy_auth_source() is None


def test_agy_probe_ignores_non_cli_auth_log(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "server-20260526.log").write_text(
        "OAuth: authenticated successfully as user@example.com\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", log_dir)

    assert detect_agy_auth_source() is None


def test_agy_probe_ignores_account_warning_without_auth_success(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "cli-20260526_165607.log").write_text(
        "W0526 16:56:14.463557 47678 server_oauth.go:99] Account ineligible: "
        "Your current account is not eligible for Antigravity.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", log_dir)

    assert detect_agy_auth_source() is None


def test_agy_probe_rejects_keyring_log_when_oauth_path_is_unsafe(tmp_path, monkeypatch):
    target = tmp_path / "real-creds.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "oauth-link.json"
    link.symlink_to(target)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "cli-20260526_165607.log").write_text(
        "I0526 16:56:08.832422 47678 auth.go:114] ChainedAuth: authenticated via keyring "
        "(effective: keyring)\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", link)
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", log_dir)

    assert detect_agy_auth_source() is None


def test_agy_probe_surfaces_account_eligibility_warning(tmp_path, monkeypatch):
    wrapper = tmp_path / "fake_agy"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_AGY_PRINT}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "cli-20260526_165607.log").write_text(
        "W0526 16:56:14.463557 47678 server_oauth.go:99] Account ineligible: "
        "Your current account is not eligible for Antigravity.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", log_dir)

    cap = AgyPrintBackend(bin_override=str(wrapper)).detect()
    assert any("account eligibility warning" in warning for warning in cap.warnings)


def test_agy_probe_missing_binary_returns_warning(tmp_path, monkeypatch):
    backend = AgyPrintBackend(bin_override=str(tmp_path / "definitely-not-there"))
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", tmp_path / "no-log-dir")
    cap = backend.detect()
    # Empty path + a "not found" warning so the caller can surface a clear
    # remediation hint without the user having to read the failure trace.
    assert cap.bin_path == ""
    assert any("not found on PATH" in w for w in cap.warnings)
    assert cap.supports_print is False


def test_agy_probe_reads_model_from_antigravity_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"model": "gemini-3-pro-preview-12-2025"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_SETTINGS_PATH", settings)
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_GEMINI_SETTINGS_PATH", tmp_path / "no-gemini.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", tmp_path / "no-log-dir")
    backend = AgyPrintBackend(bin_override=None)
    cap = backend.detect()
    assert cap.model == "gemini-3-pro-preview-12-2025"


def test_agy_probe_reads_nested_model_object(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": {"name": "from-nested"}}), encoding="utf-8")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_SETTINGS_PATH", settings)
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_GEMINI_SETTINGS_PATH", tmp_path / "no-gemini.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", tmp_path / "no-log-dir")
    backend = AgyPrintBackend(bin_override=None)
    cap = backend.detect()
    assert cap.model == "from-nested"


def test_agy_probe_ignores_malformed_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text("not-json {", encoding="utf-8")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_SETTINGS_PATH", settings)
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_GEMINI_SETTINGS_PATH", tmp_path / "no-gemini.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", tmp_path / "no-log-dir")
    backend = AgyPrintBackend(bin_override=None)
    cap = backend.detect()
    assert cap.model is None


def test_agy_version_parsers():
    assert _parse_version("1.0.0\n") == "1.0.0"
    assert _parse_version("1.2.3-beta+sha.abc\n") == "1.2.3-beta+sha.abc"
    assert _parse_version("hello\nworld\n") is None
    assert _parse_version("") is None
    assert _parse_version_from_help("agy version: 4.5.6 (build)") == "4.5.6"
    assert _parse_version_from_help("no version mentioned") is None


# ---------------------------------------------------------------------------
# GeminiCliBackend probe
# ---------------------------------------------------------------------------


def test_gemini_probe_against_fake_help(tmp_path):
    wrapper = tmp_path / "fake_gemini"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GEMINI}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    backend = GeminiCliBackend(bin_override=str(wrapper))
    cap = backend.detect()
    assert cap.bin_path == str(wrapper)
    assert cap.backend == "gemini"
    assert cap.supports_print is True
    assert cap.supports_sandbox is True
    assert cap.supports_conversation is True
    assert cap.supports_streaming is True
    assert cap.supports_tool_events is True


def test_gemini_probe_missing_binary_returns_warning(tmp_path):
    backend = GeminiCliBackend(bin_override=str(tmp_path / "no-gemini"))
    cap = backend.detect()
    assert cap.bin_path == ""
    assert any("not found on PATH" in w for w in cap.warnings)


# ---------------------------------------------------------------------------
# Capability caching
# ---------------------------------------------------------------------------


def test_detect_caches_until_refresh(tmp_path, monkeypatch):
    wrapper = tmp_path / "fake_agy"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_AGY_PRINT}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    backend = AgyPrintBackend(bin_override=str(wrapper))

    calls = {"n": 0}
    orig = backend._probe

    def counting_probe():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(backend, "_probe", counting_probe)
    backend.detect()
    backend.detect()
    backend.detect(refresh=True)
    assert calls["n"] == 2
