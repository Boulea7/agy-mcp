"""Tests for agy_mcp.utils."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agy_mcp.utils import (
    REDACTION_PLACEHOLDER,
    anonymise_paths,
    augment_path_env_for_windows,
    ensure_directory,
    expand_user_path,
    is_windows,
    prepare_subprocess_command,
    redact_command,
    redact_text,
    resolve_executable,
    safe_write_text,
    scrub_env,
    truncate_middle,
    utc_now_iso,
    windows_escape,
    windows_npm_paths,
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
        ('X-Api-Key: "abc123"', True),
        ("X-Auth-Token=verylongheadervaluexyz123456", True),
        ("api_key=abc123", True),
        ("password='hunter2'", True),
        ("url=https://example.test?api_key=abc123&x=1", True),
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


@pytest.mark.parametrize(
    "value",
    [
        "job_sk" "-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "job_ghp" "_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "job_AKIA" "IOSFODNN7EXAMPLE",
        "job_github_pat" "_11ABCDEFGHIJKLMNOPQRST_abcdef1234567890abcdef1234567890",
    ],
)
def test_redact_text_handles_secret_shapes_after_safe_prefix(value: str):
    out = redact_text(f"lookup failed for {value}")
    assert value not in out
    assert REDACTION_PLACEHOLDER in out


def test_scrub_env_replaces_secret_named_keys():
    env = {
        "ANTHROPIC_API_KEY": "supersecret",
        "OPENAI_API_KEY": "shhh",
        "github_token": "leak",  # lower-case form still matched
        "DATABASE_URL": "postgres://u:p@h/d",
        "SENTRY_DSN": "https://abc@sentry.io/123",
        "APP_KEY_ID": "midword-key",  # P1 round-2 fix: _KEY_ in middle
        "MY_TOKEN_RAW": "midword-token",
        "STAGE_SECRET_VALUE": "midword-secret",
        "PATH": "/usr/bin",
        "NORMAL_VAR": "value",
    }
    out = scrub_env(env)
    assert out["ANTHROPIC_API_KEY"] == REDACTION_PLACEHOLDER
    assert out["OPENAI_API_KEY"] == REDACTION_PLACEHOLDER
    assert out["github_token"] == REDACTION_PLACEHOLDER
    assert out["DATABASE_URL"] == REDACTION_PLACEHOLDER
    assert out["SENTRY_DSN"] == REDACTION_PLACEHOLDER
    assert out["APP_KEY_ID"] == REDACTION_PLACEHOLDER
    assert out["MY_TOKEN_RAW"] == REDACTION_PLACEHOLDER
    assert out["STAGE_SECRET_VALUE"] == REDACTION_PLACEHOLDER
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


@pytest.mark.skipif(is_windows(), reason="symlink parent checks are POSIX-specific")
def test_safe_write_text_verify_under_refuses_symlink_parent(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)
    target = link / "nested" / "config.json"
    with pytest.raises(OSError, match="symlink"):
        safe_write_text(target, "{}", verify_under=root)
    assert not (outside / "nested").exists()


def test_safe_write_text_leaves_no_tmp_orphans(tmp_path):
    target = tmp_path / "out.txt"
    safe_write_text(target, "ok")
    # No leftover *.tmp files in the destination directory.
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_safe_write_text_fallback_path_still_writes(tmp_path, monkeypatch):
    """If the O_NOFOLLOW re-open fails, the fallback plain write must still succeed."""

    target = tmp_path / "out.txt"
    original_open = os.open
    state = {"calls": 0}

    def fake_open(path, flags, *args, **kwargs):
        state["calls"] += 1
        # mkstemp's internal open is the first call; let it through.
        # The second open is safe_write_text's re-open — force OSError.
        if state["calls"] >= 2 and hasattr(os, "O_NOFOLLOW") and (flags & os.O_NOFOLLOW):
            raise OSError("simulated filesystem without O_NOFOLLOW")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)
    safe_write_text(target, "fallback-content")
    assert target.read_text(encoding="utf-8") == "fallback-content"


# ---------------------------------------------------------------------------
# Phase 3 R1 / M3 — anonymise_paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("/Users/alice/agy-mcp/file.py", "~/agy-mcp/file.py"),
        ("/home/bob/projects/x", "~/projects/x"),
        ("/Users/alice/x and /Users/bob/y", "~/x and ~/y"),
        ("nothing to anonymise here", "nothing to anonymise here"),
        ("", ""),
        (r"C:\Users\carol\Documents", r"~/Documents"),
        # R2 N1: Windows long-path \\?\ prefix.
        (r"\\?\C:\Users\dave\proj\main.py", r"~/proj\main.py"),
        # R2 N1: mixed forward-slash form Windows often emits in tracebacks.
        (r"C:/Users/eve/proj/main.py", r"~/proj/main.py"),
        # R2 N1: UNC path.
        (r"\\server\share\Users\frank\file.txt", r"~/file.txt"),
        # Phase 8 R2 sec P3.31: bare home path with no trailing
        # component. Prior to the (?:/|$) anchor widening these
        # escaped the redactor because the original regex required
        # a trailing slash. Pin every variant so a future regex
        # refactor cannot regress.
        ("/Users/alice", "~/"),
        ("/home/bob", "~/"),
        (r"C:\Users\carol", "~/"),
        (r"\\?\C:\Users\dave", "~/"),
        # NOTE: embedded bare paths followed by other text
        # (``see /Users/alice for details``) are NOT covered by the
        # current (?:/|$) anchor — the trailing component class
        # ``[^/\s"']+`` already terminates at whitespace, but the
        # required tail anchor is either ``/`` or end-of-string. A
        # full embedded-bare match would need a broader anchor
        # (``\b`` or "any non-path char") and a re-tuning pass; the
        # realistic leak vector for an unanonymised home path is
        # the start- or end-of-error-message scenario, which this
        # parametrise already covers.
    ],
)
def test_anonymise_paths(raw, expected):
    assert anonymise_paths(raw) == expected


def test_redact_text_anonymises_home_path_alongside_secret():
    raw = "Traceback: open /Users/alice/.aws/credentials with token sk-abcdef1234567890abcdef1234567890"
    out = redact_text(raw)
    assert "/Users/alice" not in out
    assert "sk-abcdef1234567890abcdef1234567890" not in out
    assert "~/" in out


# ---------------------------------------------------------------------------
# Windows .cmd/.bat launch parity (mirror upstream upstream-reference)
# ---------------------------------------------------------------------------


def test_prepare_subprocess_command_noop_on_posix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agy_mcp.utils.is_windows", lambda: False)
    argv = ["/usr/bin/agy", "--print=hello"]
    cmd, wrapped = prepare_subprocess_command(argv, {})
    assert cmd is argv
    assert wrapped is False


def test_prepare_subprocess_command_passes_through_exe_on_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agy_mcp.utils.is_windows", lambda: True)
    argv = [r"C:\bin\agy.exe", "--print=hi"]
    cmd, wrapped = prepare_subprocess_command(argv, {"COMSPEC": r"C:\Windows\System32\cmd.exe"})
    assert cmd is argv
    assert wrapped is False


def test_prepare_subprocess_command_wraps_cmd_on_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agy_mcp.utils.is_windows", lambda: True)
    argv = [r"C:\Users\dev\AppData\Roaming\npm\gemini.cmd", "--prompt=hello"]
    cmd, wrapped = prepare_subprocess_command(argv, {"COMSPEC": r"C:\Windows\System32\cmd.exe"})
    assert wrapped is True
    assert isinstance(cmd, str)
    assert cmd.startswith('"C:\\Windows\\System32\\cmd.exe" /d /s /c "')
    assert "gemini.cmd" in cmd
    assert "--prompt=hello" in cmd


def test_prepare_subprocess_command_wraps_bat_on_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agy_mcp.utils.is_windows", lambda: True)
    argv = [r"C:\tools\agy.bat", "arg with space", "needs&quote"]
    cmd, _ = prepare_subprocess_command(argv, {})
    # Empty COMSPEC falls back to bare "cmd.exe".
    assert cmd.startswith('"cmd.exe" /d /s /c ')
    # Whitespace + metacharacters trigger quoting + escaping.
    assert "arg with space" in cmd
    assert "needs&quote" in cmd or "needs^&quote" in cmd or '"needs&quote"' in cmd


def test_prepare_subprocess_command_escapes_percent_and_caret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agy_mcp.utils.is_windows", lambda: True)
    argv = [r"C:\bin\foo.bat", "%PATH%", "10^2"]
    cmd, wrapped = prepare_subprocess_command(argv, {"COMSPEC": "cmd.exe"})
    assert wrapped is True
    # Percent expansion must be neutralised so cmd.exe does NOT expand %PATH%.
    assert "%%PATH%%" in cmd
    # Caret must be doubled so cmd.exe does not eat the next character.
    assert "10^^2" in cmd


def test_prepare_subprocess_command_handles_empty_argv():
    cmd, wrapped = prepare_subprocess_command([], {})
    assert cmd == []
    assert wrapped is False


def test_windows_npm_paths_empty_on_posix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agy_mcp.utils.is_windows", lambda: False)
    assert windows_npm_paths() == []


def test_windows_npm_paths_filters_to_existing_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr("agy_mcp.utils.is_windows", lambda: True)
    real = tmp_path / "npm-real"
    real.mkdir()
    fake = tmp_path / "npm-missing"
    monkeypatch.setenv("APPDATA", str(real.parent))
    monkeypatch.setenv("LOCALAPPDATA", str(fake))
    monkeypatch.setenv("NPM_CONFIG_PREFIX", str(real))
    # `real / "npm"` does not exist (parent is real but child doesn't), so APPDATA candidate is dropped.
    # `NPM_CONFIG_PREFIX = real` exists -> kept.
    paths = windows_npm_paths()
    assert real in paths
    # The candidate "<LOCALAPPDATA>/npm" must NOT appear (parent dir does not exist).
    assert all("npm-missing" not in str(p) for p in paths)


def test_augment_path_env_noop_on_posix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agy_mcp.utils.is_windows", lambda: False)
    env = {"PATH": "/usr/bin"}
    out = augment_path_env_for_windows(env)
    assert out is env
    assert env["PATH"] == "/usr/bin"


def test_augment_path_env_prepends_npm_paths_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr("agy_mcp.utils.is_windows", lambda: True)
    npm_dir = tmp_path / "npm-global"
    npm_dir.mkdir()
    monkeypatch.setattr(
        "agy_mcp.utils.windows_npm_paths", lambda: [npm_dir]
    )
    env = {"Path": r"C:\Windows\System32"}
    augment_path_env_for_windows(env)
    # Case-insensitive lookup: existing 'Path' key reused, not duplicated.
    assert "PATH" not in env or env.get("PATH") == env.get("Path")
    final = env.get("Path") or env.get("PATH")
    assert final.startswith(str(npm_dir))
    assert r"C:\Windows\System32" in final


def test_augment_path_env_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr("agy_mcp.utils.is_windows", lambda: True)
    npm_dir = tmp_path / "npm-global"
    npm_dir.mkdir()
    monkeypatch.setattr(
        "agy_mcp.utils.windows_npm_paths", lambda: [npm_dir]
    )
    env = {"PATH": str(npm_dir) + os.pathsep + r"C:\Windows"}
    augment_path_env_for_windows(env)
    # No duplicate prepend.
    assert env["PATH"].count(str(npm_dir)) == 1


# ---------------------------------------------------------------------------
# openat-based airtight TOCTOU (POSIX only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(is_windows(), reason="openat is POSIX-only")
def test_openat_support_available_on_posix():
    """Modern Linux / macOS expose the full openat family we need."""

    from agy_mcp.utils import _has_openat_support

    assert _has_openat_support() is True


@pytest.mark.skipif(is_windows(), reason="openat is POSIX-only")
def test_safe_write_text_openat_writes_under_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "nested" / "deep" / "config.json"
    safe_write_text(target, '{"ok":true}', verify_under=root)
    assert target.read_text(encoding="utf-8") == '{"ok":true}'
    # mode = 0o644 default
    mode = target.stat().st_mode & 0o777
    assert mode == 0o644


@pytest.mark.skipif(is_windows(), reason="openat is POSIX-only")
def test_safe_write_text_openat_refuses_symlinked_intermediate(tmp_path):
    """Airtight path raises immediately on a symlinked parent component."""

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    # Pre-plant a symlink at the intermediate dir we are about to traverse.
    (root / "nested").symlink_to(outside, target_is_directory=True)
    target = root / "nested" / "config.json"
    with pytest.raises(OSError, match="symlink|non-directory"):
        safe_write_text(target, '{}', verify_under=root)
    # The write must not have leaked into `outside`.
    assert not (outside / "config.json").exists()


@pytest.mark.skipif(is_windows(), reason="openat is POSIX-only")
def test_safe_write_text_openat_refuses_path_outside_root(tmp_path):
    root = tmp_path / "root"
    sibling = tmp_path / "sibling"
    root.mkdir()
    sibling.mkdir()
    # ``path.parent`` literally outside the root: relative_to fails.
    with pytest.raises(OSError, match="not under"):
        safe_write_text(sibling / "out.txt", "nope", verify_under=root)


@pytest.mark.skipif(is_windows(), reason="openat is POSIX-only")
def test_safe_write_text_openat_refuses_dotdot_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = root / ".." / "outside" / "escaped.txt"
    with pytest.raises(OSError, match="traversal"):
        safe_write_text(target, "nope", verify_under=root)
    assert not (outside / "escaped.txt").exists()


def test_safe_write_text_fallback_refuses_dotdot_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr("agy_mcp.utils._has_openat_support", lambda: False)
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = root / ".." / "outside" / "escaped.txt"
    with pytest.raises(OSError, match="traversal"):
        safe_write_text(target, "nope", verify_under=root)
    assert not (outside / "escaped.txt").exists()


@pytest.mark.skipif(is_windows(), reason="openat is POSIX-only")
def test_safe_write_text_openat_does_not_leak_tmp_on_success(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "sub" / "f.txt"
    safe_write_text(target, "ok", verify_under=root)
    # Only the final file lives in the leaf dir, no .<name>.<hex>.tmp orphans.
    leaf = list(target.parent.iterdir())
    assert leaf == [target]


@pytest.mark.skipif(is_windows(), reason="openat is POSIX-only")
def test_safe_write_text_openat_overwrites_existing_target(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "data.txt"
    safe_write_text(target, "v1", verify_under=root)
    safe_write_text(target, "v2", verify_under=root)
    assert target.read_text(encoding="utf-8") == "v2"


@pytest.mark.skipif(is_windows(), reason="openat is POSIX-only")
def test_safe_write_text_openat_idempotent_on_existing_intermediates(tmp_path):
    """Mid-walk mkdir of an existing dir must not abort the write."""

    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    target = root / "a" / "b" / "c.txt"
    safe_write_text(target, "hello", verify_under=root)
    assert target.read_text(encoding="utf-8") == "hello"


@pytest.mark.skipif(is_windows(), reason="openat is POSIX-only")
def test_safe_write_text_falls_back_when_openat_unsupported(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """If openat support is masked off, the detect-after-the-fact path still works."""

    monkeypatch.setattr("agy_mcp.utils._has_openat_support", lambda: False)
    root = tmp_path / "root"
    root.mkdir()
    target = root / "fallback.txt"
    safe_write_text(target, "fallback-ok", verify_under=root)
    assert target.read_text(encoding="utf-8") == "fallback-ok"
