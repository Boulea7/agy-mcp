"""Tests for agy_mcp.supervisor — async job lifecycle, cancellation, sink wiring."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

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
from agy_mcp.supervisor import StoreEventSink, Supervisor


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
    ) -> None:
        super().__init__()
        self._cap = capability
        self.backend = capability.backend
        self._events = events
        self._exit_code = exit_code
        self._delay = delay_per_event
        self._block_until_cancel = block_until_cancel
        self._spawn_raises = spawn_raises

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
        )


def _capability(
    bin_path: str = "/fake/scripted",
    *,
    backend: BackendName = "agy",
    supports_log_file: bool = False,
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
        authenticated=True,
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

    persisted = supervisor.read(response.job_id)
    assert [e.type for e in persisted] == ["system", "assistant", "result"]


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
    events_persisted = supervisor.read(response.job_id)
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
    tail = supervisor.read(response.job_id, since=2)
    assert [e.type for e in tail] == ["assistant", "result"]


def test_read_translates_when_requested(tmp_path: Path):
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
    translated = supervisor.read(response.job_id, translate="raw")
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
