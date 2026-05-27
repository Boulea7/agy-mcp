"""Tests for agy_mcp.supervisor — async job lifecycle, cancellation, sink wiring."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from agy_mcp.adapters.base import AdapterRunResult, BaseAdapter, EventSink
from agy_mcp.config import BackendConfig, Config, ExecuteConfig, SafetyConfig
from agy_mcp.models import (
    BackendName,
    BridgeRequest,
    CanonicalEvent,
    Capability,
)
from agy_mcp.safety import SafetyPolicy
from agy_mcp.session_store import SessionStore
from agy_mcp.supervisor import (
    _RECONCILE_ERROR,
    StoreEventSink,
    Supervisor,
    _linux_process_start_signature,
    _migrate_if_present,
    _pid_exists,
    _process_start_signature,
    _worktree_slug,
)
from agy_mcp.worktree import WorktreeHandle, cleanup_worktree

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _ScriptedAdapter(BaseAdapter):
    """Adapter that emits a scripted event sequence then returns a chosen result.

    The script is consumed inside ``run`` — each event is forwarded to the
    sink to exercise the on-disk event log + redaction chain.
    """

    backend: BackendName = "agy"

    def __init__(
        self,
        *,
        capability: Capability,
        events: list[CanonicalEvent],
        exit_code: int = 0,
        delay_per_event: float = 0.0,
        block_until_cancel: bool = False,
        spawn_raises: Exception | None = None,
        had_upstream_error: bool = False,
        upstream_error_text: str | None = None,
    ) -> None:
        super().__init__()
        self._cap = capability
        self.backend = capability.backend
        self._events = events
        self._exit_code = exit_code
        self._delay = delay_per_event
        self._block_until_cancel = block_until_cancel
        self._spawn_raises = spawn_raises
        self._had_upstream_error = had_upstream_error
        self._upstream_error_text = upstream_error_text
        self.run_requests: list[BridgeRequest] = []

    def _probe(self) -> Capability:
        return self._cap

    def build_command(self, request: BridgeRequest, *, log_path: Path | None) -> list[str]:
        return ["/fake/scripted"]

    def run(
        self,
        request: BridgeRequest,
        *,
        log_path: Path | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        event_sink: EventSink | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AdapterRunResult:
        self.run_requests.append(request)
        if self._spawn_raises is not None:
            raise self._spawn_raises
        forwarded: list[CanonicalEvent] = []
        for event in self._events:
            if cancel_event is not None and cancel_event.is_set():
                event = CanonicalEvent(
                    type="error",
                    subtype="cancelled",
                    text="cancelled mid-script",
                )
            if event_sink is not None:
                event_sink.emit(event)
            forwarded.append(event)
            if self._delay > 0:
                time.sleep(self._delay)
        if self._block_until_cancel:
            # Wait for cancel up to a generous test cap so a hung test
            # still terminates.
            assert cancel_event is not None, "block_until_cancel requires cancel_event"
            cancel_event.wait(timeout=5)
            cancel_marker = CanonicalEvent(
                type="result",
                subtype="cancelled",
                text="job cancelled by supervisor",
            )
            if event_sink is not None:
                event_sink.emit(cancel_marker)
            forwarded.append(cancel_marker)
            return AdapterRunResult(
                events=forwarded,
                session_id=request.session_id,
                exit_code=None,
                duration_ms=0,
                stdout_tail="",
                stderr_tail="",
                log_path=None,
                artifacts=[],
            )
        return AdapterRunResult(
            events=forwarded,
            session_id=request.session_id or "sess-scripted",
            exit_code=self._exit_code,
            duration_ms=0,
            stdout_tail="",
            stderr_tail="",
            log_path=None,
            artifacts=[],
            had_upstream_error=self._had_upstream_error,
            upstream_error_text=self._upstream_error_text,
        )


def _capability(
    bin_path: str = "/fake/scripted",
    *,
    backend: BackendName = "agy",
    supports_log_file: bool = False,
    authenticated: bool = True,
) -> Capability:
    return Capability(
        bin_path=bin_path,
        backend=backend,
        version="1.0.0",
        supports_print=True,
        supports_print_timeout=True,
        supports_conversation=True,
        supports_log_file=supports_log_file,
        supports_streaming=False,
        supports_tool_events=False,
        model=None,
        authenticated=authenticated,
        warnings=[],
    )


def _default_config(session_root: Path) -> Config:
    cfg = Config()
    cfg.execute = ExecuteConfig(worktree_default=False)
    cfg.backend = BackendConfig(prefer="agy", output_protocol="claude")
    cfg.safety = SafetyConfig()
    cfg.session_store.root = str(session_root)
    return cfg


def _supervisor_with(
    adapter: BaseAdapter, *, tmp_path: Path, route_warnings: list[str] | None = None,
) -> Supervisor:
    config = _default_config(tmp_path)
    safety = SafetyPolicy.from_config(config)
    store = SessionStore(tmp_path)

    def _factory(request, cfg, sft):
        return adapter, list(route_warnings or [])

    return Supervisor(
        store=store, config=config, safety=safety, adapter_factory=_factory,
    )


def _wait_for(predicate, *, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


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
# StoreEventSink
# ---------------------------------------------------------------------------


def test_store_event_sink_persists_events(tmp_path: Path):
    store = SessionStore(tmp_path)
    record = store.create_job(cwd=str(tmp_path))
    sink = StoreEventSink(store, record.job_id)
    sink.emit(CanonicalEvent(type="assistant", text="hello"))
    sink.emit(CanonicalEvent(type="result", subtype="success"))
    events = store.read_events(record.job_id)
    assert [e.type for e in events] == ["assistant", "result"]


def test_store_event_sink_swallows_io_error(tmp_path: Path, monkeypatch):
    """The sink must never raise; an OSError from the store has to be eaten
    so the adapter run isn't poisoned."""

    store = SessionStore(tmp_path)
    record = store.create_job(cwd=str(tmp_path))
    sink = StoreEventSink(store, record.job_id)

    def _bad_append(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "append_event", _bad_append)
    sink.emit(CanonicalEvent(type="assistant", text="will not raise"))


# ---------------------------------------------------------------------------
# Supervisor.start happy path
# ---------------------------------------------------------------------------


def test_start_returns_running_envelope_and_completes(tmp_path: Path):
    events = [
        CanonicalEvent(type="system", subtype="init"),
        CanonicalEvent(type="assistant", text="ok"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="hello", cwd=str(tmp_path))
    response = supervisor.start(request)

    assert response.success is True
    assert response.status == "running"
    assert response.job_id and response.job_id.startswith("job_")
    assert response.adapter.backend == "agy"

    # Wait until the worker finishes.
    assert _wait_for(
        lambda: supervisor.status(response.job_id).status in ("completed", "failed"),
    )
    record = supervisor.status(response.job_id)
    assert record.status == "completed"
    assert record.exit_code == 0
    assert record.session_id == "sess-scripted"

    persisted = supervisor.read_events(response.job_id)
    assert [e.type for e in persisted] == ["system", "assistant", "result"]


def test_start_records_supervisor_owner_signature(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "agy_mcp.supervisor._process_start_signature",
        lambda pid: "current-owner" if pid == os.getpid() else None,
    )
    adapter = _ScriptedAdapter(capability=_capability(), events=[])
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    response = supervisor.start(BridgeRequest(prompt="hello", cwd=str(tmp_path)))

    assert response.success is True
    record = supervisor.store.get_job(response.job_id)
    assert record is not None
    assert record.pid == os.getpid()
    assert record.extra["supervisor"] == {
        "pid": os.getpid(),
        "instance_id": supervisor._instance_id,
        "process_start_signature": "current-owner",
    }


def test_start_redacts_request_snapshot(tmp_path: Path):
    events = [
        CanonicalEvent(type="assistant", text="ok"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    raw_bearer = "abcdef123456abcdef123456abcdef"
    request = BridgeRequest(
        prompt=f"Authorization: Bearer {raw_bearer}",
        cwd=str(tmp_path),
        extra_env={"MY_TOKEN": "plain-extra-env-secret"},
    )
    response = supervisor.start(request)
    assert response.success is True
    record = supervisor.status(response.job_id)
    snapshot = json.dumps(record.request, sort_keys=True)
    assert raw_bearer not in snapshot
    assert "plain-extra-env-secret" not in snapshot
    assert record.request["extra_env"] == {"MY_TOKEN": "***"}


def test_start_redacts_public_cwd_but_runs_adapter_in_raw_cwd(tmp_path: Path):
    events = [
        CanonicalEvent(type="assistant", text="ok"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    raw_cwd = "/Users/alice/private-project"
    response = supervisor.start(BridgeRequest(prompt="hello", cwd=raw_cwd))
    assert response.success is True
    assert response.cwd == "~/private-project"

    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "completed",
    )
    assert adapter.run_requests[0].cwd == raw_cwd
    record = supervisor.status(response.job_id)
    assert record.cwd == "~/private-project"
    listed = supervisor.list_sessions(limit=10)
    assert listed[0].cwd == "~/private-project"
    persisted = supervisor.store.get_job(response.job_id)
    assert persisted is not None
    assert persisted.cwd == "~/private-project"


def test_start_short_circuits_when_backend_unavailable(tmp_path: Path):
    cap = _capability(bin_path="")
    cap.warnings = ["agy missing"]
    adapter = _ScriptedAdapter(capability=cap, events=[])
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    response = supervisor.start(request)
    assert response.success is False
    assert "agy" in (response.error or "")
    # No job dir should have been created.
    assert not any(tmp_path.iterdir())


def test_start_running_envelope_redacts_adapter_bin_path(tmp_path: Path):
    raw_bin_path = str(Path.home() / ".local" / "bin" / "agy")
    adapter = _ScriptedAdapter(
        capability=_capability(bin_path=raw_bin_path),
        events=[CanonicalEvent(type="assistant", text="hi")],
        delay_per_event=0.01,
    )
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)

    response = supervisor.start(BridgeRequest(prompt="hi", cwd=str(tmp_path)))

    assert response.success is True
    assert response.adapter.bin_path == "~/.local/bin/agy"
    assert raw_bin_path not in response.model_dump_json()
    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "completed",
    )


def test_start_short_circuits_when_agy_unauthenticated(tmp_path: Path):
    cap = _capability(authenticated=False)
    cap.warnings = ["OAuth credentials missing"]
    adapter = _ScriptedAdapter(capability=cap, events=[])
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path), backend="agy")
    response = supervisor.start(request)
    assert response.success is False
    assert "not authenticated" in (response.error or "")
    assert not adapter.run_requests
    assert not any(tmp_path.iterdir())


def test_start_applies_safety_gate_before_adapter_selection(tmp_path: Path):
    config = _default_config(tmp_path / "sessions")
    safety = SafetyPolicy.from_config(config)
    store = SessionStore(tmp_path / "sessions")
    called = False

    def _factory(request, cfg, sft):
        nonlocal called
        called = True
        raise AssertionError("adapter selection should not run")

    supervisor = Supervisor(
        store=store, config=config, safety=safety, adapter_factory=_factory,
    )
    request = BridgeRequest(prompt="please rm -rf /", cwd=str(tmp_path))
    response = supervisor.start(request)
    assert response.success is False
    assert "destructive" in (response.error or "")
    assert called is False
    assert not any((tmp_path / "sessions").iterdir())


def test_start_execute_runs_inside_retained_worktree(tmp_path: Path):
    repo = _init_git_repo(tmp_path / "repo")
    events = [
        CanonicalEvent(type="assistant", text="ok"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    config = _default_config(tmp_path / "sessions")
    config.execute = ExecuteConfig(worktree_default=True)
    safety = SafetyPolicy.from_config(config)
    store = SessionStore(tmp_path / "sessions")

    def _factory(request, cfg, sft):
        return adapter, []

    supervisor = Supervisor(
        store=store, config=config, safety=safety, adapter_factory=_factory,
    )
    request = BridgeRequest(
        prompt="update README",
        cwd=str(repo),
        mode="execute",
        allow_write=True,
        session_id="sess-iso",
    )
    response = supervisor.start(request)
    assert response.success is True
    assert response.cwd != str(repo)
    assert ".agy-mcp/worktrees/sess-iso" in response.cwd
    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "completed",
    )
    assert adapter.run_requests
    run_cwd = Path(adapter.run_requests[0].cwd)
    assert run_cwd == Path(response.cwd)
    assert run_cwd.exists()
    record = supervisor.status(response.job_id)
    assert record.cwd == str(run_cwd)

    cleanup_worktree(
        WorktreeHandle(
            path=run_cwd,
            branch="agy-mcp/sess-iso",
            base_repo=repo,
            base_ref="HEAD",
        ),
        force=True,
    )


def test_status_marks_crashed_worker_as_failed(tmp_path: Path):
    """If the worker thread dies without finalizing (impossible in normal
    code paths but worth covering), status() must rewrite the record."""

    events = [CanonicalEvent(type="assistant", text="will crash")]
    adapter = _ScriptedAdapter(
        capability=_capability(), events=events,
        spawn_raises=RuntimeError("simulated crash"),
    )
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    response = supervisor.start(request)

    assert _wait_for(
        lambda: supervisor.status(response.job_id).status in ("completed", "failed"),
    )
    record = supervisor.status(response.job_id)
    assert record.status == "failed"
    assert "simulated crash" in (record.error or "")


def test_status_keeps_foreign_live_supervisor_job_running(tmp_path: Path):
    """A second process must not mark another live supervisor's job failed."""

    adapter = _ScriptedAdapter(capability=_capability(), events=[])
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    record = supervisor.store.create_job(
        job_id="job_foreign_live",
        cwd=str(tmp_path),
        pid=os.getpid(),
        extra={"supervisor": {"pid": os.getpid(), "instance_id": "foreign"}},
    )

    public = supervisor.status(record.job_id)

    assert public.status == "running"
    assert supervisor.store.get_job(record.job_id).status == "running"


def test_status_keeps_foreign_live_supervisor_with_matching_signature(
    tmp_path: Path,
    monkeypatch,
):
    """A matching process signature prevents PID-reuse false positives."""

    monkeypatch.setattr(
        "agy_mcp.supervisor._process_start_signature",
        lambda pid: "current-owner" if pid == os.getpid() else None,
    )
    adapter = _ScriptedAdapter(capability=_capability(), events=[])
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    record = supervisor.store.create_job(
        job_id="job_foreign_live_signed",
        cwd=str(tmp_path),
        pid=os.getpid(),
        extra={
            "supervisor": {
                "pid": os.getpid(),
                "instance_id": "foreign",
                "process_start_signature": "current-owner",
            }
        },
    )

    public = supervisor.status(record.job_id)

    assert public.status == "running"
    assert supervisor.store.get_job(record.job_id).status == "running"


def test_status_reconciles_foreign_reused_pid(tmp_path: Path, monkeypatch):
    """A reused PID must not keep a stale foreign job running forever."""

    monkeypatch.setattr(
        "agy_mcp.supervisor._process_start_signature",
        lambda pid: "current-owner" if pid == os.getpid() else None,
    )
    adapter = _ScriptedAdapter(capability=_capability(), events=[])
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    record = supervisor.store.create_job(
        job_id="job_foreign_reused_pid",
        cwd=str(tmp_path),
        pid=os.getpid(),
        extra={
            "supervisor": {
                "pid": os.getpid(),
                "instance_id": "foreign",
                "process_start_signature": "previous-owner",
            }
        },
    )

    public = supervisor.status(record.job_id)

    assert public.status == "failed"
    assert public.error == _RECONCILE_ERROR
    assert supervisor.store.get_job(record.job_id).status == "failed"


def test_process_start_signature_uses_timezone_stable_ps_env(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Mon Jan  1 00:00:00 2024\n",
            stderr="",
        )

    monkeypatch.setattr("agy_mcp.supervisor.subprocess.run", _fake_run)

    assert (
        _process_start_signature(123_456_789)
        == "ps-lstart:Mon Jan  1 00:00:00 2024"
    )
    assert captured["cmd"] == ["ps", "-o", "lstart=", "-p", "123456789"]
    assert isinstance(captured["env"], dict)
    assert captured["env"]["TZ"] == "UTC"
    assert captured["env"]["LC_ALL"] == "C"


def test_linux_process_start_signature_uses_boot_id_and_start_ticks(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_pid = proc_root / "123"
    proc_boot = proc_root / "sys" / "kernel" / "random"
    proc_pid.mkdir(parents=True)
    proc_boot.mkdir(parents=True)
    fields_after_comm = ["S"] + ["0"] * 19
    fields_after_comm[19] = "42424242"
    (proc_pid / "stat").write_text(
        f"123 (python worker) {' '.join(fields_after_comm)}\n",
        encoding="utf-8",
    )
    (proc_boot / "boot_id").write_text("boot-id-123\n", encoding="utf-8")

    assert _linux_process_start_signature(123, proc_root=proc_root) == (
        "proc-stat:boot-id-123:42424242"
    )


def test_pid_exists_uses_non_destructive_windows_probe(monkeypatch):
    calls: list[int] = []

    def _forbid_signal_probe(pid: int, signal: int) -> None:
        raise AssertionError("os.kill must not be used for Windows PID probes")

    def _fake_windows_pid_exists(pid: int) -> bool:
        calls.append(pid)
        return True

    monkeypatch.setattr("agy_mcp.supervisor.os.name", "nt")
    monkeypatch.setattr("agy_mcp.supervisor.os.kill", _forbid_signal_probe)
    monkeypatch.setattr(
        "agy_mcp.supervisor._windows_pid_exists",
        _fake_windows_pid_exists,
    )

    assert _pid_exists(123) is True
    assert calls == [123]


def test_status_reconciles_foreign_dead_supervisor_job(tmp_path: Path):
    """A dead foreign owner is still reconciled as a stale running job."""

    adapter = _ScriptedAdapter(capability=_capability(), events=[])
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    record = supervisor.store.create_job(
        job_id="job_foreign_dead",
        cwd=str(tmp_path),
        pid=999_999_999,
        extra={"supervisor": {"pid": 999_999_999, "instance_id": "foreign"}},
    )

    public = supervisor.status(record.job_id)

    assert public.status == "failed"
    assert public.error == _RECONCILE_ERROR
    assert supervisor.store.get_job(record.job_id).status == "failed"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancel_signals_running_job(tmp_path: Path):
    events = [
        CanonicalEvent(type="system", subtype="init"),
    ]
    adapter = _ScriptedAdapter(
        capability=_capability(), events=events, block_until_cancel=True,
    )
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="long-running", cwd=str(tmp_path))
    response = supervisor.start(request)

    # Give the worker a moment to enter the block.
    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "running",
        timeout=2.0,
    )
    assert supervisor.cancel(response.job_id) is True
    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "cancelled",
    )
    record = supervisor.status(response.job_id)
    assert record.status == "cancelled"
    events_persisted = supervisor.read_events(response.job_id)
    assert any(e.subtype == "cancelled" for e in events_persisted)


def test_cancel_on_unknown_job_returns_false(tmp_path: Path):
    events = [CanonicalEvent(type="assistant", text="done")]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    assert supervisor.cancel("job_does_not_exist") is False


def test_cancel_on_finished_job_returns_false(tmp_path: Path):
    events = [
        CanonicalEvent(type="assistant", text="quick"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="quick", cwd=str(tmp_path))
    response = supervisor.start(request)
    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "completed",
    )
    assert supervisor.cancel(response.job_id) is False


# ---------------------------------------------------------------------------
# Read / list / since-offset
# ---------------------------------------------------------------------------


def test_read_since_offset_returns_only_new_events(tmp_path: Path):
    events = [
        CanonicalEvent(type="system", subtype="init"),
        CanonicalEvent(type="assistant", text="one"),
        CanonicalEvent(type="assistant", text="two"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    response = supervisor.start(request)
    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "completed",
    )
    tail = supervisor.read_events(response.job_id, since=2)
    assert [e.type for e in tail] == ["assistant", "result"]


def test_read_translated_emits_dicts(tmp_path: Path):
    events = [
        CanonicalEvent(type="assistant", text="translated"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    response = supervisor.start(request)
    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "completed",
    )
    translated = supervisor.read_translated(response.job_id, protocol="raw")
    assert isinstance(translated, list)
    assert all(isinstance(e, dict) for e in translated)


def test_list_sessions_returns_recent_first(tmp_path: Path):
    events = [
        CanonicalEvent(type="assistant", text="done"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    job_ids: list[str] = []
    for _ in range(3):
        resp = supervisor.start(request)
        assert _wait_for(
            lambda jid=resp.job_id: supervisor.status(jid).status == "completed",
        )
        job_ids.append(resp.job_id)
        # SessionStore.list_jobs sorts by mtime; sleep a tick so ordering
        # is deterministic without filesystem-clock weirdness.
        time.sleep(0.01)
    listed = supervisor.list_sessions(limit=10)
    listed_ids = [r.job_id for r in listed]
    # Most recent first → job_ids[-1] is the head.
    assert listed_ids[0] == job_ids[-1]


# ---------------------------------------------------------------------------
# Route warnings + capability warnings flow through
# ---------------------------------------------------------------------------


def test_route_warnings_persist_to_job_record(tmp_path: Path):
    events = [
        CanonicalEvent(type="assistant", text="ok"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    supervisor = _supervisor_with(
        adapter, tmp_path=tmp_path,
        route_warnings=["fallback to gemini"],
    )
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    response = supervisor.start(request)
    # Initial envelope carries the warning.
    assert "fallback to gemini" in response.warnings
    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "completed",
    )
    record = supervisor.status(response.job_id)
    assert record.extra.get("route_warnings") == ["fallback to gemini"]


def test_status_redacts_session_store_paths(tmp_path: Path):
    events = [
        CanonicalEvent(type="assistant", text="ok"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    response = supervisor.start(BridgeRequest(prompt="hi", cwd=str(tmp_path)))
    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "completed",
    )

    public = supervisor.status(response.job_id)
    persisted = supervisor.store.get_job(response.job_id)
    assert persisted is not None
    assert public.events_path == f"<session:{response.job_id}/events.jsonl>"
    assert public.stdout_path == f"<session:{response.job_id}/stdout.log>"
    assert public.stderr_path == f"<session:{response.job_id}/stderr.log>"
    assert public.log_path == f"<session:{response.job_id}/agy.log>"
    assert persisted.events_path != public.events_path


def test_finalize_marks_zero_exit_upstream_error_as_failed_status(tmp_path: Path):
    upstream_msg = (
        "FAILED_PRECONDITION (code 400): User location is not supported for the API use."
    )
    events = [
        CanonicalEvent(type="error", subtype="upstream_failed_precondition", text=upstream_msg),
        CanonicalEvent(type="result", subtype="upstream_error"),
    ]
    adapter = _ScriptedAdapter(
        capability=_capability(),
        events=events,
        exit_code=0,
        had_upstream_error=True,
        upstream_error_text=upstream_msg,
    )
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    response = supervisor.start(BridgeRequest(prompt="hi", cwd=str(tmp_path)))

    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "upstream_error",
    )
    record = supervisor.status(response.job_id)
    assert record.exit_code == 0
    assert record.error == upstream_msg


# ---------------------------------------------------------------------------
# Adapter exception during run is captured as failure
# ---------------------------------------------------------------------------


def test_adapter_exception_captured_as_failure(tmp_path: Path):
    adapter = _ScriptedAdapter(
        capability=_capability(),
        events=[],
        spawn_raises=RuntimeError("upstream blew up"),
    )
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    response = supervisor.start(request)
    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "failed",
    )
    record = supervisor.status(response.job_id)
    assert "upstream blew up" in (record.error or "")
    assert record.exit_code is None


def test_adapter_exception_salvages_spool_files(tmp_path: Path):
    class _SpoolCrashAdapter(_ScriptedAdapter):
        def run(
            self,
            request: BridgeRequest,
            *,
            log_path: Path | None = None,
            stdout_path: Path | None = None,
            stderr_path: Path | None = None,
            event_sink: EventSink | None = None,
            cancel_event: threading.Event | None = None,
        ) -> AdapterRunResult:
            self.run_requests.append(request)
            assert stdout_path is not None
            assert stderr_path is not None
            assert log_path is not None
            stdout_path.write_text("stdout evidence", encoding="utf-8")
            stderr_path.write_text("stderr evidence", encoding="utf-8")
            log_path.write_text("klog evidence", encoding="utf-8")
            raise RuntimeError("adapter crashed after writing spool")

    adapter = _SpoolCrashAdapter(
        capability=_capability(supports_log_file=True),
        events=[],
    )
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    response = supervisor.start(BridgeRequest(prompt="hi", cwd=str(tmp_path)))

    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "failed",
    )
    record = supervisor.status(response.job_id)
    assert "adapter crashed" in (record.error or "")
    persisted = supervisor.store.get_job(response.job_id)
    assert persisted is not None
    assert Path(persisted.stdout_path).read_text(encoding="utf-8") == "stdout evidence"
    assert Path(persisted.stderr_path).read_text(encoding="utf-8") == "stderr evidence"
    assert Path(persisted.log_path).read_text(encoding="utf-8") == "klog evidence"


# ---------------------------------------------------------------------------
# Phase 4 R1 regressions
# ---------------------------------------------------------------------------


def test_start_unavailable_envelope_redacts_path_warning(tmp_path: Path):
    """P1.1 (sec): synchronous unavailable envelope must redact $HOME paths."""

    cap = _capability(bin_path="")
    cap.warnings = [f"OAuth credentials missing at {Path.home() / '.gemini' / 'oauth_creds.json'}"]
    adapter = _ScriptedAdapter(capability=cap, events=[])
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    response = supervisor.start(request)
    assert response.success is False
    # The /Users/<u>/ → ~/ anonymisation strips the operator's username.
    assert "/Users/" not in " ".join(response.warnings)
    assert "/home/" not in " ".join(response.warnings)


def test_start_rejects_duplicate_job_id(tmp_path: Path):
    """P1.2 (sec): explicit duplicate job_id surfaces a structured failure."""

    events = [
        CanonicalEvent(type="assistant", text="hi"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    first = supervisor.start(request, job_id="job_pinned_for_test")
    assert first.success is True
    assert _wait_for(
        lambda: supervisor.status(first.job_id).status == "completed",
    )
    # Second start with the same job_id must NOT overwrite — must error.
    second = supervisor.start(request, job_id="job_pinned_for_test")
    assert second.success is False
    assert "already exists" in (second.error or "")


def test_status_does_not_downgrade_completed_record(tmp_path: Path):
    """P0#1 (arch): a completed record must never be flipped to failed."""

    events = [
        CanonicalEvent(type="assistant", text="quick"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    supervisor = _supervisor_with(adapter, tmp_path=tmp_path)
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    response = supervisor.start(request)
    assert _wait_for(
        lambda: supervisor.status(response.job_id).status == "completed",
    )
    # status() called after the worker has popped its handle must still
    # return ``completed`` (not ``failed: worker thread exited without
    # finalize``). The lock-internal re-read protects against this race.
    record = supervisor.status(response.job_id)
    assert record.status == "completed"
    assert record.error is None


def test_migrate_refuses_symlink_destination(tmp_path: Path):
    """P2.3 (sec): a planted symlink at the destination must not be followed."""

    src = tmp_path / "src.txt"
    src.write_text("payload", encoding="utf-8")
    secret_target = tmp_path / "secret.txt"
    secret_target.write_text("DO NOT OVERWRITE", encoding="utf-8")
    dst = tmp_path / "dst.txt"
    dst.symlink_to(secret_target)

    _migrate_if_present(src, dst)

    # The symlink itself is still present; the secret target was not written
    # through the symlink. The migrate is a no-op on the symlink case.
    assert dst.is_symlink()
    assert secret_target.read_text(encoding="utf-8") == "DO NOT OVERWRITE"


def test_route_warnings_redacted_in_running_envelope(tmp_path: Path):
    """P1.1 (sec): warnings on the success path also go through redact."""

    events = [
        CanonicalEvent(type="assistant", text="ok"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _ScriptedAdapter(capability=_capability(), events=events)
    home = str(Path.home())
    supervisor = _supervisor_with(
        adapter, tmp_path=tmp_path,
        route_warnings=[f"resolved binary at {home}/.local/bin/agy"],
    )
    request = BridgeRequest(prompt="hi", cwd=str(tmp_path))
    response = supervisor.start(request)
    assert response.success is True
    # /Users/<u>/ collapses to ~/ in the warnings list.
    assert all(home not in w for w in response.warnings)


# ---------------------------------------------------------------------------
# v0.1.5 — _worktree_slug uniqueness
# ---------------------------------------------------------------------------


def test_worktree_slug_always_carries_job_id_suffix():
    """v0.1.5: two concurrent jobs sharing the same ``session_id`` must
    derive different worktree slugs so they can't collide on the same
    ``<repo>/.agy-mcp/worktrees/<slug>/`` path."""

    req = BridgeRequest(prompt="hello", session_id="conv-shared")
    slug_a = _worktree_slug(req, "job_111111_aaaaaaaaaaaa")
    slug_b = _worktree_slug(req, "job_111111_bbbbbbbbbbbb")
    assert slug_a != slug_b
    # The sanitiser preserves underscores, so the job-id suffix appears
    # verbatim inside the slug.
    assert "job_111111_aaaaaaaaaaaa" in slug_a
    assert "job_111111_bbbbbbbbbbbb" in slug_b
    # The session seed is also present so operators can still recognise
    # which conversation a worktree belongs to.
    assert slug_a.startswith("conv-shared")
    assert slug_b.startswith("conv-shared")


def test_worktree_slug_without_session_falls_back_to_job_id():
    """v0.1.5: without a caller-supplied session_id, the slug derives
    entirely from the job_id so each job still owns a unique worktree."""

    req = BridgeRequest(prompt="hi")
    slug_a = _worktree_slug(req, "job_222222_111111111111")
    slug_b = _worktree_slug(req, "job_222222_222222222222")
    assert slug_a != slug_b
    assert "111111111111" in slug_a
    assert "222222222222" in slug_b


def test_worktree_slug_caps_length_at_80_chars():
    """v0.1.5: even with the maximum-length session_id allowed by
    BridgeRequest (96 chars), the worktree module's 80-char invariant
    must hold so ``_validate_session_name`` accepts the result."""

    long_session = "s" * 90  # within BridgeRequest's 96-char cap
    req = BridgeRequest(prompt="hi", session_id=long_session)
    slug = _worktree_slug(req, "job_333333_cccccccccccc")
    assert 1 <= len(slug) <= 80
