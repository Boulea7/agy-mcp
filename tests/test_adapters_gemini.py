"""Tests for GeminiCliBackend — stream-json parsing + run() integration."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from agy_mcp.adapters.agy import _RunContext
from agy_mcp.adapters.base import ListEventSink
from agy_mcp.adapters.gemini import (
    GeminiCliBackend,
    _first_field,
    _translate_gemini_event,
)
from agy_mcp.models import BridgeRequest


HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
FAKE_GEMINI = FIXTURES / "fake_gemini_streamjson.py"


def _wrapper(tmp_path: Path, name: str = "fake_gemini") -> Path:
    p = tmp_path / name
    p.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GEMINI}" "$@"\n',
        encoding="utf-8",
    )
    p.chmod(0o755)
    return p


def _ctx() -> _RunContext:
    return _RunContext(
        stdout_buf=[],
        stderr_buf=[],
        events=[],
        seen_session_id=[None],
        stop_event=threading.Event(),
        sink=None,
        transcript_seen=set(),
    )


@pytest.fixture(autouse=True)
def _clear_fake_env(monkeypatch):
    for k in list(os.environ.keys()):
        if k.startswith("GEMINI_TEST_") or k.startswith("FAKE_GEMINI_"):
            monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# build_command
# ---------------------------------------------------------------------------


def test_build_command_includes_stream_json_and_resume(tmp_path):
    wrapper = _wrapper(tmp_path)
    backend = GeminiCliBackend(bin_override=str(wrapper))
    req = BridgeRequest(
        prompt="hello", cwd=str(tmp_path), session_id="sess-1",
        sandbox=True, model="gemini-3-pro",
    )
    argv = backend.build_command(req, log_path=None)
    # H1 fix: prompt/model/resume use fused --flag=value form so a hostile
    # value starting with -- cannot peel off as a fresh flag.
    assert "--prompt=hello" in argv
    assert "-o" in argv
    assert "stream-json" in argv
    assert "--sandbox" in argv
    assert "--model=gemini-3-pro" in argv
    assert "--resume=sess-1" in argv


def test_gemini_build_command_resists_flag_prompt_injection(tmp_path):
    """Phase 3 R1 / H1 regression: hostile prompt starting with -- must not
    peel off as a fresh flag in argv."""

    wrapper = _wrapper(tmp_path)
    backend = GeminiCliBackend(bin_override=str(wrapper))
    req = BridgeRequest(prompt="--sandbox", cwd=str(tmp_path))
    argv = backend.build_command(req, log_path=None)
    # The hostile string must live INSIDE --prompt=<value>, never as a free
    # argv element where the downstream parser could consume it as a flag.
    # (--sandbox-as-real-flag may still be added by request.sandbox; here
    # request.sandbox is False so a bare "--sandbox" would prove injection.)
    assert "--sandbox" not in argv
    assert any(a == "--prompt=--sandbox" for a in argv)


def test_build_command_raises_without_binary(tmp_path):
    backend = GeminiCliBackend(bin_override=str(tmp_path / "no-gemini"))
    req = BridgeRequest(prompt="x", cwd=str(tmp_path))
    with pytest.raises(RuntimeError, match="gemini binary not found"):
        backend.build_command(req, log_path=None)


# ---------------------------------------------------------------------------
# _first_field — alias resolution
# ---------------------------------------------------------------------------


def test_first_field_picks_first_present_key():
    assert _first_field({"type": "x", "kind": "y"}, ("type", "kind", "event")) == "x"
    assert _first_field({"kind": "y"}, ("type", "kind", "event")) == "y"
    assert _first_field({"event": "z"}, ("type", "kind", "event")) == "z"
    assert _first_field({}, ("type", "kind", "event")) is None


def test_first_field_returns_none_value_if_present():
    """When the alias key exists explicitly as None, _first_field returns it."""

    assert _first_field({"type": None, "kind": "y"}, ("type", "kind")) is None


# ---------------------------------------------------------------------------
# _translate_gemini_event — synthesised payloads
# ---------------------------------------------------------------------------


def test_translate_assistant_message_uses_text_field():
    ctx = _ctx()
    evt = _translate_gemini_event(
        {"type": "message", "role": "assistant", "session_id": "sess-1", "text": "hi"},
        ctx,
    )
    assert evt is not None
    assert evt.type == "assistant"
    assert evt.text == "hi"
    assert ctx.seen_session_id[0] == "sess-1"
    assert evt.content == [{"type": "text", "text": "hi"}]


def test_translate_assistant_message_jsonifies_non_string_text():
    ctx = _ctx()
    evt = _translate_gemini_event(
        {"type": "message", "role": "assistant", "session_id": "x", "text": {"k": "v"}},
        ctx,
    )
    assert evt is not None
    assert json.loads(evt.text) == {"k": "v"}


def test_translate_turn_completed_emits_result():
    ctx = _ctx()
    evt = _translate_gemini_event(
        {"event": "turn.completed", "thread_id": "t-9"}, ctx,
    )
    assert evt is not None
    assert evt.type == "result"
    assert evt.subtype == "turn_completed"
    assert ctx.seen_session_id[0] == "t-9"


def test_translate_error_becomes_error_event():
    ctx = _ctx()
    evt = _translate_gemini_event(
        {"type": "error", "session_id": "s", "message": "boom"}, ctx,
    )
    assert evt is not None
    assert evt.type == "error"
    assert evt.text and "boom" in evt.text


def test_translate_unknown_event_is_preserved_as_subagent_event():
    ctx = _ctx()
    evt = _translate_gemini_event(
        {"type": "tool_invocation", "session_id": "s", "data": [1, 2, 3]},
        ctx,
    )
    assert evt is not None
    assert evt.type == "subagent_event"
    assert evt.subtype == "tool_invocation"
    assert evt.raw == {"type": "tool_invocation", "session_id": "s", "data": [1, 2, 3]}


# ---------------------------------------------------------------------------
# Full run() against fake_gemini_streamjson
# ---------------------------------------------------------------------------


def test_run_translates_stream_json_to_canonical_events(tmp_path, monkeypatch):
    wrapper = _wrapper(tmp_path)
    monkeypatch.setenv("GEMINI_TEST_SESSION", "thread-xyz")
    monkeypatch.setenv("GEMINI_TEST_REPLY", "stream reply")
    backend = GeminiCliBackend(bin_override=str(wrapper))
    sink = ListEventSink()
    req = BridgeRequest(prompt="hi", cwd=str(tmp_path), timeout=10)
    result = backend.run(req, event_sink=sink)

    assert result.exit_code == 0
    assert result.session_id == "thread-xyz"
    types = [(e.type, e.subtype) for e in result.events]
    # init from adapter + thread.started + user msg + assistant + turn.completed + success
    assert ("system", "init") in types
    assistant_events = [e for e in result.events if e.type == "assistant"]
    assert assistant_events and "stream reply" in (assistant_events[0].text or "")
    # success result emitted because exit code 0.
    assert result.events[-1].type == "result"
    assert result.events[-1].subtype == "success"


def test_run_surfaces_decode_failures(tmp_path, monkeypatch):
    wrapper = _wrapper(tmp_path)
    monkeypatch.setenv("GEMINI_TEST_GARBAGE", "1")
    backend = GeminiCliBackend(bin_override=str(wrapper))
    req = BridgeRequest(prompt="hi", cwd=str(tmp_path), timeout=10)
    result = backend.run(req)
    # Garbage line surfaces as an error, but parsing continues.
    decode_errors = [e for e in result.events if e.subtype == "stream_decode_failure"]
    assert decode_errors
    # And we still saw an assistant message afterwards.
    assert any(e.type == "assistant" for e in result.events)


def test_run_surfaces_upstream_error_event(tmp_path, monkeypatch):
    wrapper = _wrapper(tmp_path)
    monkeypatch.setenv("GEMINI_TEST_ERROR", "upstream said no")
    monkeypatch.setenv("GEMINI_TEST_EXIT", "1")
    backend = GeminiCliBackend(bin_override=str(wrapper))
    req = BridgeRequest(prompt="hi", cwd=str(tmp_path), timeout=10)
    result = backend.run(req)
    error_events = [
        e for e in result.events
        if e.type == "error" and "upstream" in (e.text or "")
    ]
    assert error_events
    # Final result must be error subtype (exit code 1, not timeout).
    assert result.exit_code == 1
    assert result.events[-1].type == "result"
    assert result.events[-1].subtype == "error"


def test_run_handles_spawn_failure(tmp_path, monkeypatch):
    backend = GeminiCliBackend(bin_override="/does/not/matter")
    from agy_mcp.models import Capability

    monkeypatch.setattr(
        backend,
        "detect",
        lambda refresh=False: Capability(
            bin_path="/no/such/binary", backend="gemini", supports_print=True,
            supports_streaming=True,
        ),
    )
    monkeypatch.setattr(
        backend,
        "build_command",
        lambda request, log_path: ["/no/such/binary/here"],
    )
    req = BridgeRequest(prompt="x", cwd=str(tmp_path), timeout=5)
    result = backend.run(req)
    spawn_errors = [e for e in result.events if e.subtype == "spawn_failure"]
    assert spawn_errors


# ---------------------------------------------------------------------------
# Parity with agy adapter: env scrub + cwd refusal (Round 3 P3)
# ---------------------------------------------------------------------------


def test_gemini_subprocess_env_scrubs_host_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk" "-veryverylongtoken1234567890ABCD")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak-livenotredacted")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "AKIA" "IOSFODNN7EXAMPLE")
    monkeypatch.setenv("HARMLESS_VAR", "keep-me")
    backend = GeminiCliBackend(bin_override=None)
    req = BridgeRequest(prompt="x", cwd=str(tmp_path))
    env = backend._build_subprocess_env(req)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY"):
        assert env[name] == "***"
    assert env["HARMLESS_VAR"] == "keep-me"


def test_gemini_subprocess_env_scrubs_extra_env(tmp_path, monkeypatch):
    backend = GeminiCliBackend(bin_override=None)
    req = BridgeRequest(
        prompt="x", cwd=str(tmp_path),
        extra_env={"OPENAI_API_KEY": "sk-leakedviaextra1234567890ABCD"},
    )
    env = backend._build_subprocess_env(req)
    assert env["OPENAI_API_KEY"] == "***"


def test_gemini_run_refuses_missing_cwd(tmp_path):
    backend = GeminiCliBackend(bin_override=None)
    req = BridgeRequest(prompt="x", cwd=str(tmp_path / "no-such-dir"))
    result = backend.run(req)
    assert any(e.subtype == "invalid_cwd" for e in result.events)


def test_gemini_run_refuses_file_as_cwd(tmp_path):
    f = tmp_path / "not-a-dir"
    f.write_text("x")
    backend = GeminiCliBackend(bin_override=None)
    req = BridgeRequest(prompt="x", cwd=str(f))
    result = backend.run(req)
    assert any(e.subtype == "invalid_cwd" for e in result.events)


# ---------------------------------------------------------------------------
# Comprehensive env-name coverage (Round 3 P3 #5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_OPENAI_API_KEY",
        "STRIPE_API_KEY",
        "SLACK_BOT_TOKEN",
        "NPM_TOKEN",
        "DATABASE_URL",
        "REDIS_URL",
        "MONGODB_URI",
        "SENTRY_DSN",
        "VAULT_TOKEN",
    ],
)
def test_default_scrub_env_names_all_redacted(monkeypatch, tmp_path, name):
    monkeypatch.setenv(name, "should-not-leak-1234567890")
    backend = GeminiCliBackend(bin_override=None)
    env = backend._build_subprocess_env(BridgeRequest(prompt="x", cwd=str(tmp_path)))
    assert env[name] == "***", f"{name} not scrubbed"
