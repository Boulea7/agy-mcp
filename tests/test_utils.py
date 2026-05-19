"""Tests for agy_mcp.utils."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agy_mcp.utils import (
    REDACTION_PLACEHOLDER,
    ensure_directory,
    expand_user_path,
    is_windows,
    redact_command,
    redact_text,
    resolve_executable,
    safe_write_text,
    scrub_env,
    truncate_middle,
    utc_now_iso,
    windows_escape,
)


# ---------------------------------------------------------------------------
# utc_now_iso
# ---------------------------------------------------------------------------


def test_utc_now_iso_is_sortable():
    a = utc_now_iso()
    b = utc_now_iso()
    assert a <= b
    assert a.endswith("Z")
    assert "T" in a


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expect_redaction",
    [
        ("Bearer abcdef1234567890abcdef1234567890", True),
        ("Authorization: gho" "_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", True),
        ("X-Api-Key: shh-12345-secret-xyz-678", True),
        ("X-Auth-Token=verylongheadervaluexyz123456", True),
        ("sk" "-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", True),
        ("AIza" "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", True),
        ("ya29" ".aaaaaaaaaaaaaaaaaaaaaaaa", True),
        ("eyJ" "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", True),
        ("AKIA" "IOSFODNN7EXAMPLE", True),
        ("xox" "b-1234567890-abcdefghijklmn", True),
        ("github_pat" "_11ABCDEFGHIJKLMNOPQRST_abcdef1234567890abcdef1234567890abcdef1234567890", True),
        ("hello world", False),
        ("short_token", False),
    ],
)
def test_redact_text_handles_common_secret_shapes(value: str, expect_redaction: bool):
    out = redact_text(value)
    if expect_redaction:
        assert REDACTION_PLACEHOLDER in out
    else:
        assert out == value


def test_redact_text_handles_pem_block():
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDR\n"
        "-----END PRIVATE KEY-----"
    )
    out = redact_text(pem)
    assert REDACTION_PLACEHOLDER in out
    assert "BEGIN PRIVATE KEY" not in out
    assert "MIIEvQIBADANB" not in out


def test_redact_text_empty_string_returns_empty():
    assert redact_text("") == ""


def test_redact_command_redacts_each_arg():
    argv = ["agy", "--prompt", "use Bearer gho" "_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    out = redact_command(argv)
    assert REDACTION_PLACEHOLDER in out[-1]
    assert out[0] == "agy"
    assert out[1] == "--prompt"


def test_scrub_env_replaces_secret_named_keys():
    env = {
        "ANTHROPIC_API_KEY": "supersecret",
        "OPENAI_API_KEY": "shhh",
        "github_token": "leak",  # lower-case form still matched
        "DATABASE_URL": "postgres://u:p@h/d",
        "SENTRY_DSN": "https://abc@sentry.io/123",
        "PATH": "/usr/bin",
        "NORMAL_VAR": "value",
    }
    out = scrub_env(env)
    assert out["ANTHROPIC_API_KEY"] == REDACTION_PLACEHOLDER
    assert out["OPENAI_API_KEY"] == REDACTION_PLACEHOLDER
    assert out["github_token"] == REDACTION_PLACEHOLDER
    assert out["DATABASE_URL"] == REDACTION_PLACEHOLDER
    assert out["SENTRY_DSN"] == REDACTION_PLACEHOLDER
    assert out["PATH"] == "/usr/bin"
    assert out["NORMAL_VAR"] == "value"
    # Caller's env must not be mutated.
    assert env["ANTHROPIC_API_KEY"] == "supersecret"


def test_scrub_env_honours_extra_names():
    env = {"MY_INTERNAL_THING": "x", "PATH": "/usr/bin"}
    out = scrub_env(env, extra_names=("my_internal_thing",))
    assert out["MY_INTERNAL_THING"] == REDACTION_PLACEHOLDER
    assert out["PATH"] == "/usr/bin"


# ---------------------------------------------------------------------------
# truncate_middle
# ---------------------------------------------------------------------------


def test_truncate_middle_returns_input_when_under_budget():
    assert truncate_middle("abc", 10) == "abc"


def test_truncate_middle_preserves_head_and_tail():
    text = "A" * 100 + "Z" * 100
    out = truncate_middle(text, 80)
    assert out.startswith("A")
    assert out.endswith("Z")
    assert "[truncated]" in out
    assert len(out) <= 80 + len("\n...[truncated]...\n")


def test_truncate_middle_handles_pathological_marker_too_big():
    out = truncate_middle("abcdef", 3)
    assert len(out) <= 3


# ---------------------------------------------------------------------------
# Windows escape
# ---------------------------------------------------------------------------


def test_windows_escape_is_noop_on_posix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agy_mcp.utils.is_windows", lambda: False)
    raw = 'hello "world" \n test'
    assert windows_escape(raw) == raw


def test_windows_escape_translates_when_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agy_mcp.utils.is_windows", lambda: True)
    assert windows_escape("a\nb") == "a\\nb"
    assert windows_escape('say "hi"') == 'say \\"hi\\"'


# ---------------------------------------------------------------------------
# resolve_executable
# ---------------------------------------------------------------------------


def test_resolve_executable_returns_none_for_missing():
    assert resolve_executable("definitely-not-a-real-binary-xyz-12345") is None


@pytest.mark.skipif(is_windows(), reason="POSIX-only check")
def test_resolve_executable_returns_path_for_python():
    out = resolve_executable("python3")
    assert out is None or os.path.isabs(out)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_expand_user_path_resolves_home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    result = expand_user_path("~/agy")
    assert result.is_absolute()


def test_ensure_directory_creates_with_restrictive_mode(tmp_path):
    target = tmp_path / "nested" / "dir"
    ensure_directory(target)
    assert target.is_dir()
    if not is_windows():
        mode = target.stat().st_mode & 0o777
        assert mode == 0o755


def test_safe_write_text_atomic_replace(tmp_path):
    target = tmp_path / "subdir" / "file.txt"
    safe_write_text(target, "first")
    safe_write_text(target, "second")
    assert target.read_text(encoding="utf-8") == "second"
    if not is_windows():
        mode = target.stat().st_mode & 0o777
        assert mode == 0o644


@pytest.mark.skipif(is_windows(), reason="symlink TOCTOU is POSIX-specific")
def test_safe_write_text_does_not_overwrite_symlink_target(tmp_path):
    """Pre-existing symlinks at target must not be followed during write."""

    victim = tmp_path / "victim.txt"
    victim.write_text("DO NOT TOUCH", encoding="utf-8")
    target = tmp_path / "config.json"
    # Pre-create target as a symlink to victim (TOCTOU attack scenario).
    os.symlink(victim, target)
    # Write must atomically replace the symlink with a regular file, leaving
    # the victim contents untouched.
    safe_write_text(target, "new content")
    assert target.read_text(encoding="utf-8") == "new content"
    assert not target.is_symlink()
    assert victim.read_text(encoding="utf-8") == "DO NOT TOUCH"


def test_safe_write_text_leaves_no_tmp_orphans(tmp_path):
    target = tmp_path / "out.txt"
    safe_write_text(target, "ok")
    # No leftover *.tmp files in the destination directory.
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
