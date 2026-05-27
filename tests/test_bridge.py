"""Tests for agy_mcp.bridge — argparse, backend routing, worktree decision, dry-run, error envelope."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

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
    CanonicalEvent,
    Capability,
)
from agy_mcp.safety import SafetyPolicy
from agy_mcp.worktree import WorktreeHandle, cleanup_worktree

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


def _init_git_repo(path: Path) -> Path:
    """Create a git repo at ``path`` immune to the runner's git config.

    Adds ``GIT_CONFIG_NOSYSTEM`` + a per-test ``HOME`` so the system /
    user gitconfig (gpg signing, hook paths, commit templates) never
    leaks in. Phase 8 review: prior helper inherited whatever the
    developer had in ``~/.gitconfig`` and intermittently failed in
    environments with signing enforced.
    """

    path.mkdir(parents=True, exist_ok=True)
    isolated_home = path.parent / f"{path.name}.gitconfig"
    isolated_home.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(isolated_home),
        "XDG_CONFIG_HOME": str(isolated_home / ".config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    base_argv = [
        "git",
        "-c", "init.defaultBranch=main",
        "-c", "commit.gpgsign=false",
        "-c", "tag.gpgsign=false",
    ]
    subprocess.run(
        base_argv + ["init"],
        cwd=path, check=True, capture_output=True, env=env,
    )
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(
        base_argv + ["add", "README.md"],
        cwd=path, check=True, capture_output=True, env=env,
    )
    subprocess.run(
        base_argv + [
            "-c", "user.name=Test",
            "-c", "user.email=test@example.com",
            "commit", "-m", "init",
        ],
        cwd=path,
        check=True,
        capture_output=True,
        env=env,
    )
    return path


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


def test_parser_help_matches_cli_limitations():
    help_text = _build_parser().format_help()
    assert "Unsupported in CLI mode" in help_text
    assert "agy_start" in help_text
    assert "currently gemini" in help_text


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
    req, warnings = _request_from_args(args, cfg)
    assert warnings == []
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
    req, _warnings = _request_from_args(args, _default_config())
    assert req.worktree is None  # None means "follow config"


def test_request_from_args_worktree_false_is_explicit():
    parser = _build_parser()
    args = parser.parse_args(["--PROMPT", "x", "--worktree", "false"])
    req, _warnings = _request_from_args(args, _default_config())
    assert req.worktree is False


# ---------------------------------------------------------------------------
# _parse_extra_env validation
# ---------------------------------------------------------------------------


def test_parse_extra_env_accepts_valid_names():
    out, rejected = _parse_extra_env(["FOO=bar", "BAZ_QUX=1", "_LEADING=ok"])
    assert out == {"FOO": "bar", "BAZ_QUX": "1", "_LEADING": "ok"}
    assert rejected == []


def test_parse_extra_env_rejects_unsafe_keys():
    # Lowercase, leading digit, path-like, special chars all dropped — but the
    # CLI surfaces them via the returned ``rejected`` list (Phase 8 R1 sec
    # P1-2) so they are no longer SILENTLY dropped.
    inputs = [
        "lowercase=x",
        "1leading=x",
        "/etc/passwd=x",
        "FOO BAR=x",
        "FOO-BAR=x",       # hyphen disallowed
        "no-equals-here",  # no = at all
        "=missing-key",
    ]
    out, rejected = _parse_extra_env(inputs)
    assert out == {}
    assert set(rejected) == set(inputs)


def test_parse_extra_env_rejects_runtime_control_keys():
    inputs = [
        "NODE_OPTIONS=--require=/tmp/hook.js",
        "PYTHONPATH=/tmp/inject",
        "DYLD_INSERT_LIBRARIES=/tmp/lib.dylib",
        "GIT_CONFIG_GLOBAL=/tmp/gitconfig",
        "AGY_CLI_DISABLE_AUTO_UPDATE=0",
        "ANTIGRAVITY_CONVERSATION_ID=other",
        "PATH=/tmp/bin",
        "HOME=/tmp/home",
    ]
    out, rejected = _parse_extra_env(inputs)
    assert out == {}
    assert set(rejected) == set(inputs)


def test_parse_extra_env_keeps_value_verbatim_for_valid_keys():
    """Values are NOT scrubbed by _parse_extra_env itself — env-scrub happens later."""

    out, rejected = _parse_extra_env(["MY_TOKEN=secret-value-here"])
    assert out == {"MY_TOKEN": "secret-value-here"}
    assert rejected == []


def test_parse_extra_env_rejects_control_chars_in_value():
    """Phase 3 R1 / M5: newline/CR/NUL in value must drop the entry — they
    smuggle a fake second variable into the child env when echoed.
    Phase 8 R1 sec P1-2: rejected entries surface in the second tuple
    element instead of being silently lost."""

    out, rejected = _parse_extra_env(
        [
            "FOO=line1\nline2",
            "BAR=carriage\rreturn",
            "BAZ=null\x00byte",
            "OK=clean",
        ]
    )
    assert out == {"OK": "clean"}
    assert len(rejected) == 3
    assert all(r.split("=")[0] in {"FOO", "BAR", "BAZ"} for r in rejected)


def test_parse_extra_env_last_value_wins():
    out, rejected = _parse_extra_env(["FOO=first", "FOO=second"])
    assert out == {"FOO": "second"}
    assert rejected == []


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


def test_session_slug_collapses_dot_runs():
    """Phase 3 R1 / M4: ``..`` inside a slug becomes ``.`` so git refname
    validation never sees the forbidden ``..`` sequence."""

    slug = _make_session_slug("a..b")
    assert ".." not in slug
    assert slug == "a.b"


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
    meta = _adapter_meta(fake, request, _safety())
    assert meta.model == "user-pick"
    assert meta.output_protocol == "raw"
    assert meta.backend == "agy"
    assert meta.bin_path == cap.bin_path


def test_adapter_meta_falls_back_to_capability_model():
    cap = _capability("agy")
    cap.model = "auto-model"
    fake = _FakeAdapter(capability=cap, run_result=_result())
    request = BridgeRequest(prompt="x")
    meta = _adapter_meta(fake, request, _safety())
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
    # Warnings now land in resp.warnings, NOT resp.error (Phase 3 R1 P0 fix).
    assert resp.error is None
    assert resp.warnings == ["warning-one", "warning-two"]


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


def test_run_promotes_upstream_error_to_top_level(monkeypatch, tmp_path: Path):
    """v0.1.7: when the adapter detects a swallowed upstream API error
    (``had_upstream_error=True``) the BridgeResponse must surface it at
    the top level as ``success=False``, ``status='upstream_error'`` and
    ``error=<first upstream message>`` even though ``exit_code == 0``.

    Without this, MCP clients that read only ``success`` / ``status`` /
    ``error`` (rather than iterating ``all_messages``) would mistake a
    region-blocked / quota-exceeded call for a successful no-op."""

    cap = _capability("agy", supports_log_file=True)
    events = [
        CanonicalEvent(type="system", subtype="init"),
        CanonicalEvent(
            type="error",
            subtype="upstream_failed_precondition",
            text="FAILED_PRECONDITION (code 400): User location is not supported.",
        ),
        CanonicalEvent(type="result", subtype="upstream_error"),
    ]
    upstream_msg = (
        "FAILED_PRECONDITION (code 400): User location is not supported for the API use."
    )
    run_result = AdapterRunResult(
        events=events,
        session_id="sess-region-block",
        exit_code=0,
        duration_ms=5000,
        stdout_tail="",
        stderr_tail="",
        log_path=None,
        artifacts=[],
        had_upstream_error=True,
        upstream_error_text=upstream_msg,
    )
    fake = _FakeAdapter(capability=cap, run_result=run_result)

    def _build(backend, cfg, safety):
        return fake

    monkeypatch.setattr("agy_mcp.bridge._build_adapter", _build)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    resp = _run(request, _default_config(), _safety())
    assert resp.success is False
    assert resp.status == "upstream_error"
    assert resp.error == upstream_msg
    assert resp.SESSION_ID == "sess-region-block"


def test_run_keeps_success_when_no_upstream_error_and_exit_zero(
    monkeypatch, tmp_path: Path
):
    """Regression guard: had_upstream_error=False + exit_code=0 must still
    map to success=True / status=completed (don't accidentally penalise
    the happy path)."""

    cap = _capability("agy", supports_log_file=True)
    run_result = AdapterRunResult(
        events=[CanonicalEvent(type="assistant", text="ok")],
        session_id="sess-OK",
        exit_code=0,
        duration_ms=5,
        stdout_tail="",
        stderr_tail="",
        log_path=None,
        artifacts=[],
        had_upstream_error=False,
        upstream_error_text=None,
    )
    fake = _FakeAdapter(capability=cap, run_result=run_result)

    def _build(backend, cfg, safety):
        return fake

    monkeypatch.setattr("agy_mcp.bridge._build_adapter", _build)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    resp = _run(request, _default_config(), _safety())
    assert resp.success is True
    assert resp.status == "completed"
    assert resp.error is None


def test_run_redacts_public_cwd_but_keeps_adapter_cwd_raw(monkeypatch):
    cap = _capability("agy", supports_log_file=True)
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(
            events=[CanonicalEvent(type="assistant", text="ok")],
            session_id="sess-OK",
        ),
    )

    def _build(backend, cfg, safety):
        return fake

    monkeypatch.setattr("agy_mcp.bridge._build_adapter", _build)
    raw_cwd = "/Users/alice/private-project"
    request = BridgeRequest(prompt="hi", cwd=raw_cwd)
    resp = _run(request, _default_config(), _safety())
    assert resp.success is True
    assert resp.cwd == "~/private-project"
    assert fake.run_calls[0].cwd == raw_cwd


def test_run_exception_redacts_public_cwd(monkeypatch):
    def _explode(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("agy_mcp.bridge._select_backend", _explode)
    request = BridgeRequest(prompt="hello", cwd="/Users/alice/private-project")
    resp = _run(request, _default_config(), _safety())
    assert resp.success is False
    assert resp.cwd == "~/private-project"


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


def test_run_unsafe_dry_run_redacts_adapter_bin_path(monkeypatch, tmp_path: Path):
    raw_bin_path = str(Path.home() / ".local" / "bin" / "agy")
    cap = _capability("agy", bin_path=raw_bin_path)
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(),
        build_argv=[raw_bin_path, "--print", "hi"],
    )
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)

    request = BridgeRequest(prompt="hi", cwd=str(tmp_path), dry_run=True, debug=True)
    resp = _run(request, _default_config(), _safety())

    assert resp.success is True
    assert resp.adapter.bin_path == "~/.local/bin/agy"
    assert resp.command_preview == ["~/.local/bin/agy", "--print", "hi"]
    assert raw_bin_path not in resp.model_dump_json()


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
    # Phase 3 R3 L1: the failure path passes through safety.redact, so
    # absolute home paths must not leak into the envelope.
    assert "/Users/" not in (resp.error or "")
    assert "/home/" not in (resp.error or "")


def test_run_unsafe_default_worktree_fails_closed_when_no_git(
    monkeypatch, tmp_path: Path,
):
    """worktree=None + config-default-True + non-git cwd → fail CLOSED, not fall back.

    Phase 3 review H2 / P0 fix: silently writing to the main checkout when
    isolation was requested-by-default is a fail-open hole; the bridge must
    refuse the run and let the caller decide whether to disable isolation.
    """

    cap = _capability("agy")
    fake = _FakeAdapter(capability=cap, run_result=_result())
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
    assert resp.success is False
    assert "worktree creation failed" in (resp.error or "")
    assert fake.run_calls == []


def test_run_unsafe_execute_retains_worktree_after_success(monkeypatch, tmp_path: Path):
    repo = _init_git_repo(tmp_path / "repo")
    cap = _capability("agy")
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(
            events=[
                CanonicalEvent(type="assistant", text="done"),
                CanonicalEvent(type="result", subtype="success"),
            ],
            session_id="sess-bridge",
        ),
    )
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)
    request = BridgeRequest(
        prompt="update README",
        cwd=str(repo),
        mode="execute",
        allow_write=True,
        session_id="sess-bridge",
    )
    resp = _run(request, _default_config(worktree_default=True), _safety())
    assert resp.success is True
    assert fake.run_calls
    run_cwd = Path(fake.run_calls[0].cwd)
    assert run_cwd.exists()
    assert run_cwd == Path(resp.cwd)
    assert ".agy-mcp/worktrees/sess-bridge" in resp.cwd
    assert any("retained for review" in warning for warning in resp.warnings)

    cleanup_worktree(
        WorktreeHandle(
            path=run_cwd,
            branch="agy-mcp/sess-bridge",
            base_repo=repo,
            base_ref="HEAD",
        ),
        force=True,
    )


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


def test_main_detach_rejects_cli_background_mode(monkeypatch, capsys, tmp_path: Path):
    """CLI --detach cannot outlive its process; MCP agy_start owns long jobs."""

    monkeypatch.setattr("agy_mcp.bridge.get_config", lambda: _default_config())
    rc = main(
        [
            "--PROMPT", "hi",
            "--cd", str(tmp_path),
            "--detach",
        ]
    )
    out = capsys.readouterr().out
    payload = json.loads(out.splitlines()[-1])
    assert rc == 1
    assert payload["success"] is False
    assert "agy_start" in payload["error"]


def test_main_detach_rejects_invalid_request_first(monkeypatch, capsys, tmp_path: Path):
    """Phase 3 R2 N4: --detach with an invalid request (e.g. empty prompt)
    must surface the pydantic validation error, not the detach handler —
    input validation runs before detach handling."""

    monkeypatch.setattr("agy_mcp.bridge.get_config", lambda: _default_config())
    rc = main(
        [
            "--PROMPT", "   ",   # whitespace-only, BridgeRequest validator rejects
            "--cd", str(tmp_path),
            "--detach",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    payload = json.loads(out.splitlines()[-1])
    assert payload["success"] is False
    # The error must mention "prompt", not the detach handler.
    assert "prompt" in payload["error"]


def test_run_unsafe_no_backend_short_circuits(monkeypatch, tmp_path: Path):
    """Phase 3 R1 / P1.3: missing binary must NOT create a worktree before
    bailing out."""

    cap = _capability("agy", bin_path="", warnings=["agy missing"])
    fake = _FakeAdapter(capability=cap, run_result=_result())
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)
    # Force is_git_workspace to True so the safety gate doesn't short-circuit
    # for unrelated reasons.
    monkeypatch.setattr("agy_mcp.bridge.is_git_workspace", lambda cwd: True)

    request = BridgeRequest(
        prompt="x",
        cwd=str(tmp_path),
        mode="execute",
        allow_write=True,
        backend="agy",
    )
    resp = _run(request, _default_config(worktree_default=True), _safety())
    assert resp.success is False
    assert fake.run_calls == []
    # Capability warning is preserved for the caller's diagnostic.
    assert any("agy missing" in w for w in resp.warnings)


def test_run_unsafe_agy_unauthenticated_short_circuits(monkeypatch, tmp_path: Path):
    raw_home_path = str(Path.home() / ".gemini" / "oauth_creds.json")
    cap = _capability(
        "agy",
        authenticated=False,
        warnings=[f"OAuth credentials missing at {raw_home_path}"],
    )
    fake = _FakeAdapter(capability=cap, run_result=_result())
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)
    request = BridgeRequest(prompt="x", cwd=str(tmp_path), backend="agy")
    resp = _run(request, _default_config(worktree_default=False), _safety())
    assert resp.success is False
    assert "not authenticated" in (resp.error or "")
    assert raw_home_path not in " ".join(resp.warnings)
    assert "~/.gemini/oauth_creds.json" in " ".join(resp.warnings)
    assert fake.run_calls == []


def test_run_unsafe_adapter_run_raises_returns_structured_envelope(
    monkeypatch, tmp_path: Path,
):
    """Phase 3 R1 / P1.1: an exception escaping adapter.run() must be
    translated into a BridgeResponse so warnings + adapter metadata survive."""

    raw_bin_path = str(Path.home() / ".local" / "bin" / "agy")
    cap = _capability("agy", bin_path=raw_bin_path)
    fake = _FakeAdapter(capability=cap, run_result=_result())

    def _explode(*args, **kwargs):
        raise RuntimeError("kaboom from inside adapter.run")

    monkeypatch.setattr(fake, "run", _explode)
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)

    request = BridgeRequest(prompt="x", cwd=str(tmp_path))
    resp = _run(request, _default_config(worktree_default=False), _safety())
    assert resp.success is False
    assert "kaboom" in (resp.error or "")
    # Adapter metadata must still be populated, not stripped to defaults.
    assert resp.adapter.backend == "agy"
    assert resp.adapter.bin_path == "~/.local/bin/agy"
    assert raw_bin_path not in resp.model_dump_json()


def test_run_unsafe_warnings_field_is_populated_on_success(
    monkeypatch, tmp_path: Path,
):
    """Phase 3 R1 / P0: success runs put advisory text in `warnings`, never
    in `error`."""

    cap = _capability("agy", warnings=["a capability warning"])
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(
            events=[
                CanonicalEvent(type="system", subtype="init"),
                CanonicalEvent(type="assistant", text="done"),
                CanonicalEvent(type="result", subtype="success"),
            ],
            session_id="sess-warn",
        ),
    )
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)
    request = BridgeRequest(prompt="x", cwd=str(tmp_path))
    resp = _run(request, _default_config(worktree_default=False), _safety())
    assert resp.success is True
    assert resp.error is None  # never carries warnings on success
    assert "a capability warning" in resp.warnings


def test_run_unsafe_redacts_success_warnings(monkeypatch, tmp_path: Path):
    raw_home_path = str(Path.home() / ".local" / "bin" / "agy")
    cap = _capability("agy", warnings=[f"resolved binary at {raw_home_path}"])
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(
            events=[
                CanonicalEvent(type="assistant", text="done"),
                CanonicalEvent(type="result", subtype="success"),
            ],
        ),
    )
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)

    resp = _run(
        BridgeRequest(prompt="x", cwd=str(tmp_path)),
        _default_config(worktree_default=False),
        _safety(),
    )

    assert resp.success is True
    assert raw_home_path not in " ".join(resp.warnings)
    assert "~/.local/bin/agy" in " ".join(resp.warnings)


def test_run_unsafe_truncates_agent_messages(monkeypatch, tmp_path: Path):
    huge_text = "A" * 120
    cap = _capability("agy")
    fake = _FakeAdapter(
        capability=cap,
        run_result=_result(
            events=[
                CanonicalEvent(type="assistant", text=huge_text),
                CanonicalEvent(type="result", subtype="success"),
            ],
        ),
    )
    monkeypatch.setattr("agy_mcp.bridge._build_adapter", lambda *a, **kw: fake)
    request = BridgeRequest(prompt="x", cwd=str(tmp_path), max_output_chars=40)
    resp = _run(request, _default_config(worktree_default=False), _safety())
    assert resp.success is True
    assert len(resp.agent_messages) <= 40
    assert "...[truncated]..." in resp.agent_messages
    assert any("agent_messages truncated" in warning for warning in resp.warnings)


def test_run_debug_traceback_anonymises_home_path(monkeypatch, tmp_path: Path):
    """Phase 3 R1 / M3: tracebacks must not leak /Users/<u>/ in the envelope."""

    def _explode(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr("agy_mcp.bridge._select_backend", _explode)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path), debug=True)
    resp = _run(request, _default_config(), _safety())
    assert resp.success is False
    err = resp.error or ""
    # The traceback section is non-empty in debug mode AND the home prefix
    # is collapsed to ``~/``.
    assert "| tb=" in err
    assert "/Users/" not in err
    assert "/home/" not in err
