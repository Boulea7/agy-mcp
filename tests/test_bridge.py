"""Tests for agy_mcp.bridge — argparse, backend routing, worktree decision, dry-run, error envelope."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agy_mcp.adapters.base import (
    AdapterRunResult,
    BaseAdapter,
    EventSink,
)
from agy_mcp.bridge import (
    _SESSION_SLUG_RE,
    _adapter_meta,
    _build_adapter,
    _build_parser,
    _dry_run_response,
    _make_session_slug,
    _parse_extra_env,
    _pick_assistant_text,
    _pick_error_text,
    _request_from_args,
    _run,
    _run_unsafe,
    _select_backend,
    _wants_worktree,
    _with_cwd,
    main,
)
from agy_mcp.config import (
    BackendConfig,
    Config,
    ExecuteConfig,
    SafetyConfig,
)
from agy_mcp.models import (
    BackendName,
    BridgeRequest,
    BridgeResponse,
    CanonicalEvent,
    Capability,
)
from agy_mcp.safety import SafetyPolicy


# ---------------------------------------------------------------------------
# Helpers — fake adapter that records what was asked of it
# ---------------------------------------------------------------------------


class _FakeAdapter(BaseAdapter):
    """Test double for BaseAdapter — never spawns a subprocess."""

    backend: BackendName = "agy"

    def __init__(
        self,
        *,
        capability: Capability,
        run_result: AdapterRunResult,
        build_argv: list[str] | None = None,
        build_raises: Exception | None = None,
        safety: SafetyPolicy | None = None,
    ) -> None:
        super().__init__(safety=safety)
        self._cap = capability
        self.backend = capability.backend
        self._run_result = run_result
        self._build_argv = build_argv or ["/fake/agy", "--print", "hello"]
        self._build_raises = build_raises
        self.build_calls: list[BridgeRequest] = []
        self.run_calls: list[BridgeRequest] = []

    def _probe(self) -> Capability:
        return self._cap

    def build_command(self, request: BridgeRequest, *, log_path: Path | None) -> list[str]:
        self.build_calls.append(request)
        if self._build_raises is not None:
            raise self._build_raises
        return list(self._build_argv)

    def run(
        self,
        request: BridgeRequest,
        *,
        log_path: Path | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        event_sink: EventSink | None = None,
    ) -> AdapterRunResult:
        self.run_calls.append(request)
        return self._run_result


def _capability(
    backend: BackendName = "agy",
    *,
    bin_path: str = "/fake/agy",
    authenticated: bool = True,
    supports_print: bool = True,
    supports_streaming: bool = False,
    supports_log_file: bool = True,
    warnings: list[str] | None = None,
) -> Capability:
    return Capability(
        bin_path=bin_path,
        backend=backend,
        version="1.0.0",
        supports_print=supports_print,
        supports_print_timeout=True,
        supports_conversation=True,
        supports_continue=False,
        supports_sandbox=True,
        supports_log_file=supports_log_file,
        supports_add_dir=False,
        supports_dangerously_skip_permissions=False,
        supports_streaming=supports_streaming,
        supports_tool_events=supports_streaming,
        model=None,
        authenticated=authenticated,
        warnings=list(warnings or []),
    )


def _result(
    *,
    events: list[CanonicalEvent] | None = None,
    session_id: str | None = "sess-1",
    exit_code: int = 0,
) -> AdapterRunResult:
    return AdapterRunResult(
        events=events or [],
        session_id=session_id,
        exit_code=exit_code,
        duration_ms=10,
        stdout_tail="",
        stderr_tail="",
        log_path=None,
        artifacts=[],
    )


def _default_config(*, worktree_default: bool = True) -> Config:
    cfg = Config()
    cfg.execute = ExecuteConfig(worktree_default=worktree_default)
    cfg.backend = BackendConfig(prefer="auto", output_protocol="claude")
    cfg.safety = SafetyConfig()
    return cfg


def _safety() -> SafetyPolicy:
    return SafetyPolicy(config=SafetyConfig())


# ---------------------------------------------------------------------------
# argparse — _build_parser + _request_from_args
# ---------------------------------------------------------------------------


def test_parser_required_prompt_flag():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--cd", "."])


def test_parser_round_trip_defaults():
    parser = _build_parser()
    args = parser.parse_args(["--PROMPT", "hi"])
    assert args.PROMPT == "hi"
    assert args.cd == "."
    assert args.mode == "ask"
    assert args.SESSION_ID is None
    assert args.timeout == 900
    assert args.worktree == "default"
    assert args.backend is None
    assert args.output_protocol is None
    assert args.allow_write is False


def test_request_from_args_honours_overrides():
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--PROMPT", "do thing",
            "--cd", "/tmp",
            "--mode", "execute",
            "--SESSION_ID", "sess-xyz",
            "--model", "gemini-3",
            "--sandbox",
            "--allow-write",
            "--worktree", "true",
            "--timeout", "60",
            "--max-output-chars", "1234",
            "--backend", "gemini",
            "--output-protocol", "codex",
            "--return-all-messages",
            "--debug",
            "--dry-run",
            "--extra-env", "MY_VAR=value",
        ]
    )
    cfg = _default_config()
    req = _request_from_args(args, cfg)
    assert req.prompt == "do thing"
    assert req.cwd == "/tmp"
    assert req.mode == "execute"
    assert req.session_id == "sess-xyz"
    assert req.model == "gemini-3"
    assert req.sandbox is True
    assert req.allow_write is True
    assert req.worktree is True
    assert req.timeout == 60
    assert req.max_output_chars == 1234
    assert req.backend == "gemini"
    assert req.output_protocol == "codex"
    assert req.return_all_messages is True
    assert req.debug is True
    assert req.dry_run is True
    assert req.extra_env == {"MY_VAR": "value"}


def test_request_from_args_worktree_default_uses_config():
    parser = _build_parser()
    args = parser.parse_args(["--PROMPT", "x"])
    req = _request_from_args(args, _default_config())
    assert req.worktree is None  # None means "follow config"


def test_request_from_args_worktree_false_is_explicit():
    parser = _build_parser()
    args = parser.parse_args(["--PROMPT", "x", "--worktree", "false"])
    req = _request_from_args(args, _default_config())
    assert req.worktree is False


# ---------------------------------------------------------------------------
# _parse_extra_env validation
# ---------------------------------------------------------------------------


def test_parse_extra_env_accepts_valid_names():
    out = _parse_extra_env(["FOO=bar", "BAZ_QUX=1", "_LEADING=ok"])
    assert out == {"FOO": "bar", "BAZ_QUX": "1", "_LEADING": "ok"}


def test_parse_extra_env_rejects_unsafe_keys():
    # Lowercase, leading digit, path-like, special chars all dropped silently.
    out = _parse_extra_env(
        [
            "lowercase=x",
            "1leading=x",
            "/etc/passwd=x",
            "FOO BAR=x",
            "FOO-BAR=x",       # hyphen disallowed
            "no-equals-here",  # no = at all
            "=missing-key",
        ]
    )
    assert out == {}


def test_parse_extra_env_keeps_value_verbatim_for_valid_keys():
    """Values are NOT scrubbed by _parse_extra_env itself — env-scrub happens later."""

    out = _parse_extra_env(["MY_TOKEN=secret-value-here"])
    assert out == {"MY_TOKEN": "secret-value-here"}


def test_parse_extra_env_last_value_wins():
    out = _parse_extra_env(["FOO=first", "FOO=second"])
    assert out == {"FOO": "second"}


# ---------------------------------------------------------------------------
# _make_session_slug
# ---------------------------------------------------------------------------


def test_session_slug_generates_random_when_seed_missing():
    a = _make_session_slug(None)
    b = _make_session_slug("")
    assert a != b
    assert a.startswith("job-")
    assert b.startswith("job-")


def test_session_slug_sanitises_path_separators():
    slug = _make_session_slug("foo/bar/../baz")
    # Path separators must be gone; literal ".." inside a single path
    # component is harmless (Path.resolve treats it as filename, not parent).
    assert "/" not in slug
    assert "\\" not in slug
    # Only [A-Za-z0-9._-] after sanitisation.
    assert _SESSION_SLUG_RE.search(slug) is None


def test_session_slug_strips_leading_dot_dash_underscore():
    slug = _make_session_slug("-.-_evil")
    assert not slug.startswith(("-", ".", "_"))


def test_session_slug_caps_length_at_80():
    slug = _make_session_slug("a" * 500)
    assert len(slug) <= 80


def test_session_slug_falls_back_to_session_when_sanitised_empty():
    """Pure punctuation seed should not collapse to empty string."""

    slug = _make_session_slug(".....")
    assert slug == "session"


# ---------------------------------------------------------------------------
# _select_backend — auto routing decision matrix
# ---------------------------------------------------------------------------


def test_select_backend_auto_picks_agy_when_authenticated(monkeypatch):
    cap_agy = _capability("agy", authenticated=True, supports_print=True)
    cap_gem = _capability("gemini", authenticated=False, supports_streaming=False)
    fake_agy = _FakeAdapter(capability=cap_agy, run_result=_result())
    fake_gem = _FakeAdapter(capability=cap_gem, run_result=_result())

    def _build(backend, cfg, safety):
        return fake_agy if backend == "agy" else fake_gem

    monkeypatch.setattr("agy_mcp.bridge._build_adapter", _build)
    request = BridgeRequest(prompt="x", backend="auto")
    adapter, warnings = _select_backend(request, _default_config(), _safety())
    assert adapter is fake_agy
    assert warnings == []


def test_select_backend_auto_falls_back_to_gemini_when_agy_unauth(monkeypatch):
    cap_agy = _capability("agy", authenticated=False, bin_path="/fake/agy")
    cap_gem = _capability("gemini", supports_streaming=True)
    fake_agy = _FakeAdapter(capability=cap_agy, run_result=_result())
    fake_gem = _FakeAdapter(capability=cap_gem, run_result=_result())

    def _build(backend, cfg, safety):
        return fake_agy if backend == "agy" else fake_gem

    monkeypatch.setattr("agy_mcp.bridge._build_adapter", _build)
    request = BridgeRequest(prompt="x", backend="auto")
    adapter, warnings = _select_backend(request, _default_config(), _safety())
    assert adapter is fake_gem
    assert any("fell back to gemini" in w for w in warnings)


def test_select_backend_auto_returns_agy_when_both_missing(monkeypatch):
    cap_agy = _capability("agy", bin_path="", supports_print=False)
    cap_gem = _capability("gemini", bin_path="", supports_streaming=False)
    fake_agy = _FakeAdapter(capability=cap_agy, run_result=_result())
    fake_gem = _FakeAdapter(capability=cap_gem, run_result=_result())

    def _build(backend, cfg, safety):
        return fake_agy if backend == "agy" else fake_gem

    monkeypatch.setattr("agy_mcp.bridge._build_adapter", _build)
    request = BridgeRequest(prompt="x", backend="auto")
    adapter, warnings = _select_backend(request, _default_config(), _safety())
    assert adapter is fake_agy
    assert any("no backend available" in w for w in warnings)


def test_select_backend_explicit_returns_requested(monkeypatch):
    cap_gem = _capability("gemini", supports_streaming=True)
    fake_gem = _FakeAdapter(capability=cap_gem, run_result=_result())

    def _build(backend, cfg, safety):
        assert backend == "gemini"
        return fake_gem

    monkeypatch.setattr("agy_mcp.bridge._build_adapter", _build)
    request = BridgeRequest(prompt="x", backend="gemini")
    adapter, warnings = _select_backend(request, _default_config(), _safety())
    assert adapter is fake_gem
    assert warnings == []


def test_select_backend_explicit_warns_when_unavailable(monkeypatch):
    cap_missing = _capability(
        "gemini", bin_path="", warnings=["binary not found"]
    )
    fake = _FakeAdapter(capability=cap_missing, run_result=_result())

    def _build(backend, cfg, safety):
        return fake

    monkeypatch.setattr("agy_mcp.bridge._build_adapter", _build)
    request = BridgeRequest(prompt="x", backend="gemini")
    adapter, warnings = _select_backend(request, _default_config(), _safety())
    assert adapter is fake
    assert any("not available" in w for w in warnings)


def test_build_adapter_rejects_unknown_backend():
    with pytest.raises(ValueError, match="unknown backend"):
        _build_adapter("bogus", _default_config(), _safety())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _wants_worktree decision matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode, allow_write, worktree_flag, worktree_default, expected",
    [
        # Non-execute: never wants worktree.
        ("ask",     True,  None,  True,  False),
        ("plan",    True,  True,  True,  False),
        # execute without allow_write: never wants worktree.
        ("execute", False, True,  True,  False),
        # execute + allow_write + explicit True
        ("execute", True,  True,  False, True),
        # execute + allow_write + explicit False overrides config.
        ("execute", True,  False, True,  False),
        # execute + allow_write + None defers to config.
        ("execute", True,  None,  True,  True),
        ("execute", True,  None,  False, False),
    ],
)
def test_wants_worktree_matrix(mode, allow_write, worktree_flag, worktree_default, expected):
    req = BridgeRequest(
        prompt="x",
        mode=mode,
        allow_write=allow_write,
        worktree=worktree_flag,
    )
    cfg = _default_config(worktree_default=worktree_default)
    assert _wants_worktree(req, cfg) is expected


# ---------------------------------------------------------------------------
# _with_cwd — model_copy semantics
# ---------------------------------------------------------------------------


def test_with_cwd_returns_new_request_with_updated_path():
    req = BridgeRequest(prompt="x", cwd="/orig")
    new = _with_cwd(req, Path("/new/path"))
    assert new is not req
    assert new.cwd == "/new/path"
    assert req.cwd == "/orig"  # original untouched
    assert new.prompt == "x"   # other fields preserved


# ---------------------------------------------------------------------------
# _adapter_meta
# ---------------------------------------------------------------------------


def test_adapter_meta_picks_request_model_over_capability():
    cap = _capability("agy")
    cap.model = "auto-model"
    fake = _FakeAdapter(capability=cap, run_result=_result())
    request = BridgeRequest(prompt="x", model="user-pick", output_protocol="raw")
    meta = _adapter_meta(fake, request)
    assert meta.model == "user-pick"
    assert meta.output_protocol == "raw"
    assert meta.backend == "agy"
    assert meta.bin_path == cap.bin_path


def test_adapter_meta_falls_back_to_capability_model():
    cap = _capability("agy")
    cap.model = "auto-model"
    fake = _FakeAdapter(capability=cap, run_result=_result())
    request = BridgeRequest(prompt="x")
    meta = _adapter_meta(fake, request)
    assert meta.model == "auto-model"


# ---------------------------------------------------------------------------
# _pick_assistant_text / _pick_error_text
# ---------------------------------------------------------------------------


def test_pick_assistant_text_returns_last_assistant():
    events = [
        CanonicalEvent(type="assistant", text="first"),
        CanonicalEvent(type="system"),
        CanonicalEvent(type="assistant", text="second"),
    ]
    assert _pick_assistant_text(events) == "second"


def test_pick_assistant_text_returns_empty_when_absent():
    assert _pick_assistant_text([]) == ""
    assert _pick_assistant_text([CanonicalEvent(type="system")]) == ""


def test_pick_error_text_prefers_error_then_failed_result():
    events = [
        CanonicalEvent(type="result", subtype="error", text="result-failed"),
        CanonicalEvent(type="error", text="explicit-error"),
    ]
    # Iterates in reverse, so explicit error wins.
    assert _pick_error_text(events) == "explicit-error"


def test_pick_error_text_returns_none_on_clean_run():
    events = [
        CanonicalEvent(type="result", subtype="success"),
        CanonicalEvent(type="assistant", text="ok"),
    ]
    assert _pick_error_text(events) is None


# ---------------------------------------------------------------------------
# _dry_run_response
# ---------------------------------------------------------------------------


def test_dry_run_response_returns_command_preview_in_debug(monkeypatch):
    cap = _capability("agy")
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(),
        build_argv=["/fake/agy", "--print", "hello"],
    )
    request = BridgeRequest(prompt="hello", debug=True, dry_run=True)
    resp = _dry_run_response(request, fake, Path("/tmp"), _safety(), [])
    assert resp.success is True
    assert resp.command_preview == ["/fake/agy", "--print", "hello"]
    assert resp.status == "completed"
    assert resp.adapter.backend == "agy"


def test_dry_run_response_hides_command_preview_when_not_debug():
    cap = _capability("agy")
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(),
        build_argv=["/fake/agy", "--print", "hello"],
    )
    request = BridgeRequest(prompt="hello", debug=False, dry_run=True)
    resp = _dry_run_response(request, fake, Path("/tmp"), _safety(), [])
    assert resp.command_preview is None


def test_dry_run_response_redacts_secrets_in_command_preview():
    """Bearer tokens in argv must be redacted before reaching the envelope."""

    cap = _capability("agy")
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(),
        build_argv=[
            "/fake/agy",
            "--header",
            "Authorization: Bearer eyJlongtokenvalueabcdef1234567890",
        ],
    )
    request = BridgeRequest(prompt="x", debug=True, dry_run=True)
    resp = _dry_run_response(request, fake, Path("/tmp"), _safety(), [])
    flat = " ".join(resp.command_preview or [])
    assert "eyJlongtokenvalueabcdef1234567890" not in flat
    assert "***" in flat


def test_dry_run_response_surfaces_build_failure():
    cap = _capability("agy")
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(),
        build_raises=RuntimeError("agy binary not found at /missing"),
    )
    request = BridgeRequest(prompt="x", debug=True, dry_run=True)
    resp = _dry_run_response(request, fake, Path("/tmp"), _safety(), [])
    assert resp.success is False
    assert "agy binary not found" in (resp.error or "")


def test_dry_run_response_concatenates_warnings():
    cap = _capability("agy")
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(),
        build_argv=["/fake/agy"],
    )
    request = BridgeRequest(prompt="x", dry_run=True)
    resp = _dry_run_response(
        request, fake, Path("/tmp"), _safety(),
        ["warning-one", "warning-two"],
    )
    assert resp.success is True
    assert resp.error == "warning-one | warning-two"


# ---------------------------------------------------------------------------
# _run / _run_unsafe — end-to-end with mocked adapter
# ---------------------------------------------------------------------------


def test_run_safety_rejection_returns_failed_envelope(tmp_path: Path, monkeypatch):
    """A destructive prompt must short-circuit before any adapter spawn."""

    # We don't actually need a working adapter — _select_backend should not
    # even be reached. But guard against accidental calls anyway.
    def _build_raises(*args, **kwargs):
        raise AssertionError("safety gate must short-circuit before adapter routing")

    monkeypatch.setattr("agy_mcp.bridge._build_adapter", _build_raises)

    request = BridgeRequest(prompt="please rm -rf / for me", cwd=str(tmp_path))
    resp = _run(request, _default_config(), _safety())
    assert resp.success is False
    assert "destructive" in (resp.error or "")


def test_run_returns_failed_envelope_on_unexpected_exception(monkeypatch, tmp_path: Path):
    """Top-level _run catches any exception and wraps it in BridgeResponse."""

    def _explode(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("agy_mcp.bridge._select_backend", _explode)
    request = BridgeRequest(prompt="hello", cwd=str(tmp_path))
    resp = _run(request, _default_config(), _safety())
    assert resp.success is False
    assert "kaboom" in (resp.error or "")


def test_run_debug_attaches_traceback(monkeypatch, tmp_path: Path):
    """In debug mode, _run appends a redacted traceback to the error field."""

    def _explode(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("agy_mcp.bridge._select_backend", _explode)
    request = BridgeRequest(prompt="hello", cwd=str(tmp_path), debug=True)
    resp = _run(request, _default_config(), _safety())
    assert resp.success is False
    assert "| tb=" in (resp.error or "")


def test_run_unsafe_success_path(monkeypatch, tmp_path: Path):
    cap = _capability("agy", supports_log_file=True)
    events = [
        CanonicalEvent(type="system", subtype="init"),
        CanonicalEvent(type="assistant", text="hi there"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(events=events, session_id="sess-OK"),
    )

    def _build(backend, cfg, safety):
        return fake

    monkeypatch.setattr("agy_mcp.bridge._build_adapter", _build)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    resp = _run(request, _default_config(), _safety())
    assert resp.success is True
    assert resp.status == "completed"
    assert resp.SESSION_ID == "sess-OK"
    assert resp.agent_messages == "hi there"
    assert resp.adapter.backend == "agy"
    # all_messages stays empty unless return_all_messages was passed.
    assert resp.all_messages == []


def test_run_unsafe_return_all_messages_populates_translated_events(
    monkeypatch, tmp_path: Path,
):
    cap = _capability("agy", supports_log_file=False)
    events = [
        CanonicalEvent(type="system", subtype="init"),
        CanonicalEvent(type="assistant", text="hi"),
    ]
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(events=events),
    )
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)
    request = BridgeRequest(
        prompt="hi",
        cwd=str(tmp_path),
        return_all_messages=True,
        output_protocol="raw",
    )
    resp = _run(request, _default_config(), _safety())
    assert resp.success is True
    assert len(resp.all_messages) == len(events)


def test_run_unsafe_failure_propagates_error_text(monkeypatch, tmp_path: Path):
    cap = _capability("agy")
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(
            events=[
                CanonicalEvent(type="error", text="upstream auth failure"),
            ],
            exit_code=2,
        ),
    )
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)
    request = BridgeRequest(prompt="x", cwd=str(tmp_path))
    resp = _run(request, _default_config(), _safety())
    assert resp.success is False
    assert resp.status == "failed"
    assert "upstream auth failure" in (resp.error or "")


def test_run_unsafe_dry_run_does_not_call_run(monkeypatch, tmp_path: Path):
    cap = _capability("agy")
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(),
        build_argv=["/fake/agy", "--print", "hi"],
    )
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path), dry_run=True, debug=True)
    resp = _run(request, _default_config(), _safety())
    assert resp.success is True
    assert resp.command_preview == ["/fake/agy", "--print", "hi"]
    assert fake.run_calls == []


def test_run_unsafe_explicit_worktree_failure_is_fatal(monkeypatch, tmp_path: Path):
    """worktree=True (explicit) MUST refuse the run if the worktree can't be created."""

    cap = _capability("agy")
    fake = _FakeAdapter(capability=cap, run_result=_result())
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)

    # tmp_path is not a git repo, so create_worktree raises.
    request = BridgeRequest(
        prompt="x",
        cwd=str(tmp_path),
        mode="execute",
        allow_write=True,
        worktree=True,
    )
    # Force is_git_workspace=True so safety.gate_request lets us through;
    # the worktree creation itself will still fail because tmp_path is not
    # a real git repo.
    monkeypatch.setattr("agy_mcp.bridge.is_git_workspace", lambda cwd: True)
    resp = _run(request, _default_config(worktree_default=True), _safety())
    assert resp.success is False
    assert "worktree creation failed" in (resp.error or "")
    assert fake.run_calls == []


def test_run_unsafe_default_worktree_falls_back_when_no_git(
    monkeypatch, tmp_path: Path,
):
    """worktree=None + config-default-True + non-git cwd → fallback (warn, run anyway)."""

    cap = _capability("agy")
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(
            events=[
                CanonicalEvent(type="system", subtype="init"),
                CanonicalEvent(type="assistant", text="done"),
                CanonicalEvent(type="result", subtype="success"),
            ],
            session_id="sess-fb",
        ),
    )
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)
    monkeypatch.setattr("agy_mcp.bridge.is_git_workspace", lambda cwd: True)

    request = BridgeRequest(
        prompt="x",
        cwd=str(tmp_path),
        mode="execute",
        allow_write=True,
        worktree=None,
    )
    resp = _run(request, _default_config(worktree_default=True), _safety())
    # Run still succeeded, but error field carries the fallback warning.
    assert resp.success is True
    assert "worktree fallback" in (resp.error or "")
    assert fake.run_calls and fake.run_calls[0].cwd == str(tmp_path.resolve())


# ---------------------------------------------------------------------------
# main() — JSON envelope on stdout + correct exit code
# ---------------------------------------------------------------------------


def test_main_emits_json_envelope_and_exit_zero(monkeypatch, capsys, tmp_path: Path):
    cap = _capability("agy")
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(
            events=[
                CanonicalEvent(type="system", subtype="init"),
                CanonicalEvent(type="assistant", text="hello"),
                CanonicalEvent(type="result", subtype="success"),
            ],
            session_id="sess-main",
        ),
    )
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)
    # Pin config so the global singleton doesn't leak between tests.
    monkeypatch.setattr("agy_mcp.bridge.get_config", lambda: _default_config())

    rc = main(
        [
            "--PROMPT", "hello",
            "--cd", str(tmp_path),
            "--mode", "ask",
            "--dry-run",
            "--debug",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out.splitlines()[-1])
    assert payload["success"] is True
    assert payload["status"] == "completed"
    assert payload["command_preview"] is not None


def test_main_returns_one_on_failure(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr("agy_mcp.bridge.get_config", lambda: _default_config())
    rc = main(
        [
            "--PROMPT", "please rm -rf /",
            "--cd", str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    payload = json.loads(out.splitlines()[-1])
    assert payload["success"] is False
    assert "destructive" in payload["error"]
