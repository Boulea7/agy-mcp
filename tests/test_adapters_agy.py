"""Tests for AgyPrintBackend — run(), klog tail, stdout buffer, error paths.

The fake agy scripts under tests/fixtures/ behave like the real binary for
the slice of surface AgyPrintBackend cares about. We bind them via
``bin_override`` and assert on the canonical event stream the adapter emits.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from agy_mcp.adapters.agy import (
    AgyPrintBackend,
    _drain_stream,
    _handle_klog_line,
    _RunContext,
)
from agy_mcp.adapters.base import ListEventSink
from agy_mcp.models import BridgeRequest, CanonicalEvent


HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
FAKE_AGY_PRINT = FIXTURES / "fake_agy_print.py"
FAKE_AGY_WITH_LOG = FIXTURES / "fake_agy_with_log.py"
SAMPLE_KLOG = FIXTURES / "sample_klog.txt"


# ---------------------------------------------------------------------------
# Shell wrappers + AGY env isolation
# ---------------------------------------------------------------------------


def _make_wrapper(tmp_path: Path, fixture: Path, name: str = "fake_agy") -> Path:
    wrapper = tmp_path / name
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{fixture}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


@pytest.fixture
def isolated_agy(tmp_path, monkeypatch):
    """Disable touching the real ~/.gemini surface during adapter tests."""

    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_SETTINGS_PATH", tmp_path / "no-settings.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_GEMINI_SETTINGS_PATH", tmp_path / "no-gemini.json")
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", tmp_path / "no-log-dir")
    # Clear AGY_TEST_* envs that previous tests may have set on os.environ.
    for key in list(os.environ.keys()):
        if key.startswith("AGY_TEST_") or key.startswith("FAKE_AGY_"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# build_command
# ---------------------------------------------------------------------------


def test_build_command_includes_print_timeout_and_log_file(tmp_path, isolated_agy):
    wrapper = _make_wrapper(tmp_path, FAKE_AGY_PRINT)
    backend = AgyPrintBackend(bin_override=str(wrapper))
    req = BridgeRequest(prompt="hello", cwd=str(tmp_path), timeout=120, sandbox=True)
    log = tmp_path / "agy.log"
    argv = backend.build_command(req, log_path=log)
    assert argv[0] == str(wrapper)
    # H1 fix: prompt is fused into --print=<value> so a hostile prompt
    # starting with -- cannot leak through as a flag.
    assert "--print=hello" in argv
    assert "--print-timeout" in argv
    idx = argv.index("--print-timeout")
    # Wrapper holds 30s back as grace window.
    assert argv[idx + 1] == "90s"
    assert "--log-file" in argv
    assert str(log) in argv
    assert "--sandbox" in argv


def test_build_command_resumes_with_conversation_id(tmp_path, isolated_agy):
    wrapper = _make_wrapper(tmp_path, FAKE_AGY_PRINT)
    backend = AgyPrintBackend(bin_override=str(wrapper))
    req = BridgeRequest(
        prompt="continue", cwd=str(tmp_path), session_id="conv-existing-123"
    )
    argv = backend.build_command(req, log_path=None)
    # H1 fix: --conversation=<id> rather than ["--conversation", id].
    assert "--conversation=conv-existing-123" in argv


def test_build_command_resists_flag_prompt_injection(tmp_path, isolated_agy):
    """Phase 3 R1 / H1 regression: hostile prompt starting with -- must not
    peel off as a fresh flag in argv."""

    wrapper = _make_wrapper(tmp_path, FAKE_AGY_PRINT)
    backend = AgyPrintBackend(bin_override=str(wrapper))
    req = BridgeRequest(
        prompt="--dangerously-skip-permissions",
        cwd=str(tmp_path),
    )
    argv = backend.build_command(req, log_path=None)
    # The hostile string must live INSIDE --print=<value>, never as a free
    # argv element where the downstream parser could consume it as a flag.
    assert "--dangerously-skip-permissions" not in argv
    assert any(a.endswith("=--dangerously-skip-permissions") for a in argv)


def test_build_command_raises_without_binary(tmp_path, monkeypatch):
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_OAUTH_CREDS_PATH", tmp_path / "no-creds.json")
    backend = AgyPrintBackend(bin_override=str(tmp_path / "missing"))
    req = BridgeRequest(prompt="x", cwd=str(tmp_path))
    with pytest.raises(RuntimeError, match="agy binary not found"):
        backend.build_command(req, log_path=None)


# ---------------------------------------------------------------------------
# run() integration with fake_agy_print
# ---------------------------------------------------------------------------


def test_run_emits_init_and_assistant_and_result(tmp_path, monkeypatch, isolated_agy):
    wrapper = _make_wrapper(tmp_path, FAKE_AGY_PRINT)
    monkeypatch.setenv("AGY_TEST_REPLY", "the answer is 42")
    backend = AgyPrintBackend(bin_override=str(wrapper))
    sink = ListEventSink()
    req = BridgeRequest(prompt="ping", cwd=str(tmp_path), timeout=60)
    log = tmp_path / "agy.log"
    result = backend.run(req, log_path=log, event_sink=sink)

    assert result.exit_code == 0
    # First event is system/init.
    assert result.events[0].type == "system"
    assert result.events[0].subtype == "init"
    # Somewhere there's an assistant event carrying the reply.
    assistants = [e for e in result.events if e.type == "assistant"]
    assert assistants and assistants[0].text.startswith("the answer is 42")
    # Last event is result/success.
    last = result.events[-1]
    assert last.type == "result"
    assert last.subtype == "success"
    assert last.metadata.get("exit_code") == 0
    # session_id was promoted from the klog "Created conversation" line.
    assert result.session_id == "12345678-aaaa-bbbb-cccc-1234567890ab"
    # Sink saw the same stream the adapter returned.
    assert len(sink.events) == len(result.events)


def test_run_surfaces_non_zero_exit_as_error_result(tmp_path, monkeypatch, isolated_agy):
    wrapper = _make_wrapper(tmp_path, FAKE_AGY_PRINT)
    monkeypatch.setenv("AGY_TEST_REPLY", "")
    monkeypatch.setenv("AGY_TEST_STDERR", "boom: connection refused")
    monkeypatch.setenv("AGY_TEST_EXIT", "2")
    backend = AgyPrintBackend(bin_override=str(wrapper))
    req = BridgeRequest(prompt="x", cwd=str(tmp_path), timeout=60)
    result = backend.run(req, log_path=tmp_path / "x.log")
    assert result.exit_code == 2
    last = result.events[-1]
    assert last.type == "result"
    assert last.subtype == "error"
    assert "boom" in (last.text or "")


def test_run_records_wrapper_timeout(tmp_path, monkeypatch, isolated_agy):
    wrapper = _make_wrapper(tmp_path, FAKE_AGY_PRINT)
    # Sleep 5s; wrapper deadline is 1s.
    monkeypatch.setenv("AGY_TEST_SLEEP", "5")
    monkeypatch.setenv("AGY_TEST_REPLY", "late")
    backend = AgyPrintBackend(bin_override=str(wrapper))
    req = BridgeRequest(prompt="slow", cwd=str(tmp_path), timeout=1)
    start = time.time()
    result = backend.run(req, log_path=tmp_path / "slow.log")
    elapsed = time.time() - start
    # Should not have run for the full 5 seconds.
    assert elapsed < 4.5
    # Two error events expected: the wrapper_timeout warning then the result.
    error_subtypes = [e.subtype for e in result.events if e.type == "error"]
    assert "wrapper_timeout" in error_subtypes
    assert result.events[-1].subtype == "wrapper_timeout"


def test_run_handles_spawn_failure(tmp_path, monkeypatch, isolated_agy):
    """If Popen raises OSError, adapter must still return a populated result."""

    backend = AgyPrintBackend(bin_override="/does/not/matter")
    monkeypatch.setattr(
        backend,
        "detect",
        lambda refresh=False: _fake_cap(bin_path="/usr/bin/no-such-bin"),
    )
    monkeypatch.setattr(
        backend,
        "build_command",
        lambda request, log_path: ["/no/such/binary/here"],
    )
    req = BridgeRequest(prompt="x", cwd=str(tmp_path), timeout=5)
    result = backend.run(req, log_path=None)
    assert result.exit_code is None
    spawn_errors = [e for e in result.events if e.subtype == "spawn_failure"]
    assert spawn_errors and "failed to spawn" in (spawn_errors[0].text or "")


def _fake_cap(bin_path: str):
    from agy_mcp.models import Capability

    return Capability(
        bin_path=bin_path,
        backend="agy",
        supports_print=True,
    )


# ---------------------------------------------------------------------------
# klog parser unit tests (independent of subprocess)
# ---------------------------------------------------------------------------


def _new_ctx() -> _RunContext:
    return _RunContext(
        stdout_buf=[],
        stderr_buf=[],
        events=[],
        seen_session_id=[None],
        stop_event=threading.Event(),
        sink=None,
        transcript_seen=set(),
    )


def test_klog_parser_extracts_grpc_port():
    ctx = _new_ctx()
    adapter = AgyPrintBackend()
    line = (
        "I0520 12:00:00.000123  1234 sidecar.go:1] "
        "Language server listening on random port at 60074 for HTTPS (gRPC)\n"
    )
    _handle_klog_line(line, ctx, adapter)
    [evt] = ctx.events
    assert evt.type == "system"
    assert evt.subtype == "sidecar_ready"
    assert evt.metadata["grpc_port"] == 60074


def test_klog_parser_extracts_conversation_id_and_promotes_session():
    ctx = _new_ctx()
    adapter = AgyPrintBackend()
    line = (
        "I0520 12:00:00.000200  1234 conv.go:1] "
        "Created conversation abc12345-dead-beef-cafe-deadbeef1234\n"
    )
    _handle_klog_line(line, ctx, adapter)
    [evt] = ctx.events
    assert evt.subtype == "conversation_started"
    assert ctx.seen_session_id[0] == "abc12345-dead-beef-cafe-deadbeef1234"
    assert evt.session_id == ctx.seen_session_id[0]


def test_klog_parser_extracts_print_starting_with_metadata():
    ctx = _new_ctx()
    adapter = AgyPrintBackend()
    line = (
        'I0520 12:00:00.000300  1234 print.go:1] Print mode: starting '
        '(promptLength=42, model="gemini-3-pro", conversationID="conv-789")\n'
    )
    _handle_klog_line(line, ctx, adapter)
    [evt] = ctx.events
    assert evt.subtype == "print_starting"
    assert evt.metadata["prompt_length"] == 42
    assert evt.metadata["model"] == "gemini-3-pro"
    assert evt.session_id == "conv-789"


def test_klog_parser_extracts_auth_timeout():
    ctx = _new_ctx()
    adapter = AgyPrintBackend()
    _handle_klog_line(
        "E0520 12:00:00.000400  1234 auth.go:1] Print mode: auth timed out\n",
        ctx, adapter,
    )
    [evt] = ctx.events
    assert evt.type == "error"
    assert evt.subtype == "auth_timeout"


def test_klog_parser_extracts_send_failure_with_redaction(monkeypatch):
    """SendUserMessage failures get redaction so they're safe to log."""

    ctx = _new_ctx()
    adapter = AgyPrintBackend()
    msg = (
        "E0520 12:00:00.000500  1234 send.go:1] "
        "Print mode: SendUserMessage failed: Authorization: Bearer eyJhbcVeryLongTokenxxxxx\n"
    )
    _handle_klog_line(msg, ctx, adapter)
    [evt] = ctx.events
    assert evt.type == "error"
    assert evt.subtype == "send_user_message_failed"
    # The bearer token must not survive verbatim into the event text.
    assert "eyJhbcVeryLongTokenxxxxx" not in (evt.text or "")


def test_klog_parser_ignores_empty_and_unrelated_lines():
    ctx = _new_ctx()
    adapter = AgyPrintBackend()
    _handle_klog_line("\n", ctx, adapter)
    _handle_klog_line(
        "I0520 12:00:00.000  1234 misc.go:9] Some unrelated info line\n",
        ctx, adapter,
    )
    assert ctx.events == []


def test_klog_parser_full_sample_log():
    """Drive the full sample log through the parser and check shape."""

    ctx = _new_ctx()
    adapter = AgyPrintBackend()
    for line in SAMPLE_KLOG.read_text(encoding="utf-8").splitlines():
        _handle_klog_line(line, ctx, adapter)

    subtypes = [e.subtype for e in ctx.events]
    # Lifecycle: sidecar → conversation_started → print_starting → flush →
    # stream_start → rewind → error → auth_timeout → auth_error → turn_end ×2.
    assert "sidecar_ready" in subtypes
    assert "conversation_started" in subtypes
    assert "print_starting" in subtypes
    assert "input_flush" in subtypes
    assert "stream_start" in subtypes
    assert "rewind" in subtypes
    assert "send_user_message_failed" in subtypes
    assert "auth_timeout" in subtypes
    assert "auth_error" in subtypes
    assert subtypes.count("turn_end") >= 1
    assert ctx.seen_session_id[0] == "12345678-aaaa-bbbb-cccc-1234567890ab"


# ---------------------------------------------------------------------------
# Integration with fake_agy_with_log (rich lifecycle stream)
# ---------------------------------------------------------------------------


def test_run_with_rich_log_promotes_session_and_emits_lifecycle(
    tmp_path, monkeypatch, isolated_agy
):
    wrapper = _make_wrapper(tmp_path, FAKE_AGY_WITH_LOG, name="fake_agy_rich")
    convo = "deadbeef-cafe-1234-5678-abcdef012345"
    monkeypatch.setenv("AGY_TEST_CONV", convo)
    monkeypatch.setenv("AGY_TEST_REPLY", "rich answer")
    backend = AgyPrintBackend(bin_override=str(wrapper))
    log = tmp_path / "rich.log"
    req = BridgeRequest(prompt="hi", cwd=str(tmp_path), timeout=20)
    result = backend.run(req, log_path=log)

    assert result.exit_code == 0
    assert result.session_id == convo
    subtypes = [e.subtype for e in result.events]
    # Adapter must surface at least the structural lifecycle events.
    for required in (
        "init",
        "sidecar_ready",
        "conversation_started",
        "print_starting",
        "turn_end",
        "success",
    ):
        assert required in subtypes, f"missing {required!r} in {subtypes!r}"


def test_run_with_log_records_auth_failure(tmp_path, monkeypatch, isolated_agy):
    wrapper = _make_wrapper(tmp_path, FAKE_AGY_WITH_LOG, name="fake_agy_authfail")
    monkeypatch.setenv("AGY_TEST_INJECT_HANG", "1")
    monkeypatch.setenv("AGY_TEST_EXIT", "1")
    backend = AgyPrintBackend(bin_override=str(wrapper))
    req = BridgeRequest(prompt="hi", cwd=str(tmp_path), timeout=20)
    result = backend.run(req, log_path=tmp_path / "auth.log")
    error_subtypes = [e.subtype for e in result.events if e.type == "error"]
    assert "auth_timeout" in error_subtypes


# ---------------------------------------------------------------------------
# _drain_stream
# ---------------------------------------------------------------------------


def test_drain_stream_writes_to_spool(tmp_path):
    import io

    ctx = _new_ctx()
    stream = io.StringIO("line one\nline two\n")
    spool = tmp_path / "stdout.spool"
    _drain_stream(stream, ctx.stdout_buf, ctx, spool, "stdout")
    assert "".join(ctx.stdout_buf) == "line one\nline two\n"
    assert spool.read_text(encoding="utf-8") == "line one\nline two\n"


def test_drain_stream_handles_missing_stream():
    ctx = _new_ctx()
    _drain_stream(None, ctx.stdout_buf, ctx, None, "stdout")  # must not raise
    assert ctx.stdout_buf == []


def test_drain_stream_refuses_symlinked_spool(tmp_path):
    """Pre-existing symlink at spool path must not be followed."""

    import io

    target = tmp_path / "real_secret.txt"
    target.write_text("untouched", encoding="utf-8")
    spool = tmp_path / "stdout.spool"
    spool.symlink_to(target)

    ctx = _new_ctx()
    stream = io.StringIO("payload\n")
    _drain_stream(stream, ctx.stdout_buf, ctx, spool, "stdout")
    # Stream content reached the in-memory buffer.
    assert "".join(ctx.stdout_buf) == "payload\n"
    # But the symlink target was never written to.
    assert target.read_text(encoding="utf-8") == "untouched"
    # And an error event was synthesised so the caller knows.
    refused = [e for e in ctx.events if e.subtype == "spool_refused"]
    assert refused


# ---------------------------------------------------------------------------
# Env scrubbing in _build_subprocess_env
# ---------------------------------------------------------------------------


def test_subprocess_env_sets_session_id_and_disables_autoupdate(
    tmp_path, monkeypatch, isolated_agy
):
    backend = AgyPrintBackend(bin_override=None)
    req = BridgeRequest(
        prompt="x",
        cwd=str(tmp_path),
        session_id="abc",
        extra_env={"MY_FLAG": "1"},
    )
    env = backend._build_subprocess_env(req)
    assert env["ANTIGRAVITY_CONVERSATION_ID"] == "abc"
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"
    assert env["MY_FLAG"] == "1"


def test_subprocess_env_scrubs_host_secrets(tmp_path, monkeypatch, isolated_agy):
    """Phase 2 review P0: confirm provider keys never reach the child."""

    monkeypatch.setenv("OPENAI_API_KEY", "sk" "-veryverylongtoken1234567890ABCD")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak-livenotredacted")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "AKIA" "IOSFODNN7EXAMPLE")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp" "_realtokenlooksuperreal")
    monkeypatch.setenv("HARMLESS_VAR", "keep-me")
    backend = AgyPrintBackend(bin_override=None)
    req = BridgeRequest(prompt="x", cwd=str(tmp_path))
    env = backend._build_subprocess_env(req)
    # Names survive so downstream tooling can still detect presence...
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"):
        assert name in env
        assert "***" == env[name], f"{name} not scrubbed"
    # ...but unrelated names are passed through unchanged.
    assert env["HARMLESS_VAR"] == "keep-me"


def test_subprocess_env_scrubs_extra_env_too(tmp_path, monkeypatch, isolated_agy):
    """Caller cannot smuggle a secret into the child via extra_env naming."""

    backend = AgyPrintBackend(bin_override=None)
    req = BridgeRequest(
        prompt="x",
        cwd=str(tmp_path),
        extra_env={"OPENAI_API_KEY": "sk-leakedviaextra1234567890ABCD"},
    )
    env = backend._build_subprocess_env(req)
    assert env["OPENAI_API_KEY"] == "***"


def test_subprocess_env_keeps_wrapper_controls_immutable(
    tmp_path, monkeypatch, isolated_agy,
):
    backend = AgyPrintBackend(bin_override=None)
    req = BridgeRequest.model_construct(
        prompt="x",
        cwd=str(tmp_path),
        session_id="real-session",
        extra_env={
            "AGY_CLI_DISABLE_AUTO_UPDATE": "0",
            "ANTIGRAVITY_CONVERSATION_ID": "fake-session",
        },
    )
    env = backend._build_subprocess_env(req)
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"
    assert env["ANTIGRAVITY_CONVERSATION_ID"] == "real-session"


# ---------------------------------------------------------------------------
# Event redaction at emit time (review P0 #2)
# ---------------------------------------------------------------------------


def test_emit_redacts_text_metadata_and_raw(tmp_path, isolated_agy):
    """Live sink must receive scrubbed events even before the translator runs."""

    from agy_mcp.adapters.base import ListEventSink

    sink = ListEventSink()
    backend = AgyPrintBackend(bin_override=None)
    ctx = _new_ctx()
    ctx.sink = sink
    # JWT pattern requires 3 dot-separated segments of >=5 chars each.
    fake_jwt = "eyJabcdef.ghijklm.nopqrstuvw"
    backend._emit(
        ctx,
        CanonicalEvent(
            type="assistant",
            text="Authorization: Bearer abcdef123456abcdef123456abcdef",
            metadata={"nested": {"jwt": fake_jwt, "ok": "fine"}},
            raw={"aws": "AKIA" "IOSFODNN7EXAMPLE"},
        ),
    )
    [evt] = sink.events
    assert "abcdef123456abcdef123456abcdef" not in (evt.text or "")
    assert evt.metadata["nested"]["jwt"] == "***"
    assert evt.metadata["nested"]["ok"] == "fine"
    assert evt.raw["aws"] == "***"


def test_emit_redacts_pydantic_extra_fields(tmp_path, isolated_agy):
    """CanonicalEvent has extra='allow'; extras must also be scrubbed."""

    from agy_mcp.adapters.base import ListEventSink

    sink = ListEventSink()
    backend = AgyPrintBackend(bin_override=None)
    ctx = _new_ctx()
    ctx.sink = sink
    backend._emit(
        ctx,
        CanonicalEvent(
            type="assistant",
            text="hi",
            # Extra kwargs land in pydantic model_extra.
            sneaky="Authorization: Bearer aaaaaaaaaaaaaaaaaaaaaaaaa",
            nested_extra={"hidden_token": "ghp_realghtokenexamplelong1234567"},
        ),
    )
    [evt] = sink.events
    extra = getattr(evt, "__pydantic_extra__", {}) or {}
    assert "aaaaaaaaaaaaaaaaaaaaaaaaa" not in str(extra.get("sneaky", ""))
    assert extra.get("nested_extra", {}).get("hidden_token") == "***"


# ---------------------------------------------------------------------------
# CWD hardening (review P1 #5)
# ---------------------------------------------------------------------------


def test_run_refuses_missing_cwd(tmp_path, isolated_agy):
    backend = AgyPrintBackend(bin_override=None)
    monkeypatch_target = tmp_path / "does-not-exist"
    req = BridgeRequest(prompt="x", cwd=str(monkeypatch_target))
    result = backend.run(req, log_path=None)
    assert result.exit_code is None
    assert result.events
    assert any(e.subtype == "invalid_cwd" for e in result.events)


def test_run_refuses_file_as_cwd(tmp_path, isolated_agy):
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("x")
    backend = AgyPrintBackend(bin_override=None)
    req = BridgeRequest(prompt="x", cwd=str(file_path))
    result = backend.run(req, log_path=None)
    assert any(e.subtype == "invalid_cwd" for e in result.events)


# ---------------------------------------------------------------------------
# klog parser tolerates extra fields (review P1 #3)
# ---------------------------------------------------------------------------


def test_klog_print_start_tolerates_extra_fields():
    """Future agy versions may add new key=value pairs; parser must not regress."""

    ctx = _new_ctx()
    adapter = AgyPrintBackend()
    line = (
        'I0520 12:00:00.000300  1234 print.go:1] Print mode: starting '
        '(promptLength=42, projectID="proj-1", model="gemini-3-pro", '
        'tenant="x", conversationID="conv-tolerant-001")\n'
    )
    _handle_klog_line(line, ctx, adapter)
    [evt] = ctx.events
    assert evt.subtype == "print_starting"
    assert evt.metadata["prompt_length"] == 42
    assert evt.metadata["model"] == "gemini-3-pro"
    assert evt.session_id == "conv-tolerant-001"
    assert evt.metadata["fields"]["projectID"] == "proj-1"
    assert evt.metadata["fields"]["tenant"] == "x"


def test_klog_created_conv_does_not_over_capture():
    """Trailing non-hex/dash chars must not be pulled into the conversation id."""

    ctx = _new_ctx()
    adapter = AgyPrintBackend()
    _handle_klog_line(
        "I0520 12:00:00.000  1234 conv.go:1] "
        "Created conversation abcdef01-2345-6789-abcd-ef0123456789 some-trailing-garbage\n",
        ctx,
        adapter,
    )
    [evt] = ctx.events
    assert evt.session_id == "abcdef01-2345-6789-abcd-ef0123456789"


def test_klog_created_conv_does_not_over_capture_trailing_dash():
    """Dash-joined trailing tokens (no whitespace) must still terminate."""

    ctx = _new_ctx()
    adapter = AgyPrintBackend()
    _handle_klog_line(
        "I0520 12:00:00.000  1234 conv.go:1] "
        "Created conversation abcd1234-cafe-extra-nonhex-suffix\n",
        ctx,
        adapter,
    )
    [evt] = ctx.events
    # ``extra`` and ``nonhex`` are not hex; capture stops at the last hex run.
    assert evt.session_id == "abcd1234-cafe"


# ---------------------------------------------------------------------------
# Transcript watcher symlink defense (review P1 #3 sec)
# ---------------------------------------------------------------------------


def test_tail_transcripts_skips_symlink_targets(tmp_path, monkeypatch):
    """A symlinked transcript.jsonl must be skipped, not drained."""

    from agy_mcp.adapters.agy import _tail_transcripts

    fake_log_root = tmp_path / "log"
    fake_log_root.mkdir()
    monkeypatch.setattr("agy_mcp.adapters.agy.AGY_LOG_DIR", fake_log_root)

    secret_outside = tmp_path / "outside_secret.txt"
    secret_outside.write_text('{"type":"would-not-want-this"}', encoding="utf-8")

    subdir = fake_log_root / "sub"
    subdir.mkdir()
    link = subdir / "transcript.jsonl"
    link.symlink_to(secret_outside)

    backend = AgyPrintBackend(bin_override=None)
    ctx = _new_ctx()
    # Run the watcher in a thread; let it complete one rglob pass, then
    # set stop_event so the wait(0.5) returns immediately.
    worker = threading.Thread(
        target=_tail_transcripts,
        args=(ctx, backend, time.time() - 60),
        daemon=True,
    )
    worker.start()
    # Give the loop a moment to enumerate, then signal stop.
    time.sleep(0.2)
    ctx.stop_event.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert ctx.events == []
    assert link in ctx.transcript_seen
