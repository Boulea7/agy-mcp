"""Supervisor — async job manager that backs ``--detach`` and the MCP long-task tools.

Responsibilities:

1. ``start`` a job: spawn ``adapter.run`` on a worker thread; persist
   the :class:`JobRecord` immediately so the caller can poll while the
   adapter is still running.
2. ``status`` / ``read`` / ``cancel`` / ``list_sessions``: read-only or
   process-controlling operations against the on-disk
   :class:`SessionStore` and the in-memory job registry.
3. Tee every :class:`CanonicalEvent` the adapter emits into the
   session store via :class:`StoreEventSink` so the live event log on
   disk stays in sync with what the supervisor reports.

Threading model:

* Each running job owns ONE worker thread executing ``adapter.run``.
* The worker also owns the spool ``TemporaryDirectory``; once the
  adapter returns the worker calls ``finalize_job`` then unlinks the
  spool dir.
* ``cancel`` flips a per-job :class:`threading.Event`; the adapter's
  wait loop polls it and walks its terminate/kill cascade.
* All bookkeeping (the ``_jobs`` registry, status writes) is guarded
  by a single :class:`threading.RLock` so MCP tool calls from the
  asyncio main loop never race against the worker threads.

Phase 4 review invariants from R3 hand-off:
* The supervisor MUST consume the adapter's event sink output, not raw
  ``stdout_buf`` / ``stderr_buf``, so the per-event redact chokepoint
  in ``BaseAdapter.emit_event`` is preserved.
* The supervisor MUST be the single writer per ``job_id`` — slug
  collisions between processes are not yet serialised; that's tracked
  for Phase 4+ work.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agy_mcp.adapters import (
    AdapterRunResult,
    BaseAdapter,
    EventSink,
    ProtocolTranslator,
)
from agy_mcp.config import Config, get_config
from agy_mcp.models import (
    AdapterMetadata,
    BackendName,
    BridgeRequest,
    BridgeResponse,
    CanonicalEvent,
    JobRecord,
    JobStatus,
)
from agy_mcp.safety import SafetyPolicy
from agy_mcp.session_store import (
    JobPaths,
    SessionStore,
    generate_job_id,
)


# ---------------------------------------------------------------------------
# Sink that tees adapter events into the on-disk event log.
# ---------------------------------------------------------------------------


class StoreEventSink(EventSink):
    """Append every received :class:`CanonicalEvent` to the session store.

    Events have already been scrubbed by ``BaseAdapter.emit_event`` before
    the sink is called, so the on-disk ``events.jsonl`` cannot persist a
    secret that survived the adapter's redaction pass.
    """

    def __init__(self, store: SessionStore, job_id: str) -> None:
        self.store = store
        self.job_id = job_id
        self._lock = threading.Lock()
        self._last_event_ts: str | None = None

    def emit(self, event: CanonicalEvent) -> None:
        with self._lock:
            try:
                self.store.append_event(self.job_id, event)
            except OSError:
                # Disk full / permission revoked / unmounted — surface as a
                # second-level error inside the event log but never raise:
                # an exception from the sink would poison the adapter run.
                return
            self._last_event_ts = event.ts


# ---------------------------------------------------------------------------
# In-memory registry entry
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _JobHandle:
    job_id: str
    cancel_event: threading.Event
    thread: threading.Thread
    started_at: float = field(default_factory=time.monotonic)
    spool_dir: Path | None = None


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


# Adapter factory signature — bridge.py and tests can inject a fake so the
# supervisor never has to import the bridge's routing logic directly.
AdapterFactory = Callable[
    [BridgeRequest, Config, SafetyPolicy],
    tuple[BaseAdapter, list[str]],
]


class Supervisor:
    """Manage async adapter runs backed by the on-disk SessionStore.

    A single instance is expected per process; the bridge's ``--detach``
    path constructs one on demand and hands the job over.
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        config: Config | None = None,
        safety: SafetyPolicy | None = None,
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        self.store = store
        self.config = config or get_config()
        self.safety = safety or SafetyPolicy.from_config(self.config)
        # Default adapter factory routes via the bridge selector so the
        # supervisor and the synchronous path stay in lockstep.
        self._adapter_factory = adapter_factory or self._default_adapter_factory
        self._jobs: dict[str, _JobHandle] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Default adapter factory (delegates to bridge._select_backend)
    # ------------------------------------------------------------------

    @staticmethod
    def _default_adapter_factory(
        request: BridgeRequest, config: Config, safety: SafetyPolicy,
    ) -> tuple[BaseAdapter, list[str]]:
        # Local import to avoid an import cycle: bridge imports Supervisor.
        from agy_mcp.bridge import _select_backend

        return _select_backend(request, config, safety)

    # ------------------------------------------------------------------
    # Public surface (called by bridge --detach and MCP tools)
    # ------------------------------------------------------------------

    def start(
        self,
        request: BridgeRequest,
        *,
        job_id: str | None = None,
    ) -> BridgeResponse:
        """Spawn a background job; return a BridgeResponse with status=running.

        Failures BEFORE the adapter spawns (adapter selection, missing
        binary) produce ``success=False`` synchronously. Failures DURING
        the adapter run are visible only via ``status`` / ``read``.
        """

        adapter, route_warnings = self._adapter_factory(
            request, self.config, self.safety,
        )
        cap = adapter.detect()
        backend_name = cap.backend

        if not cap.bin_path:
            return BridgeResponse(
                success=False,
                error=" | ".join(route_warnings) or f"backend={backend_name!r} unavailable",
                warnings=list(cap.warnings),
                cwd=request.cwd,
                adapter=AdapterMetadata(backend=backend_name),
            ).touch()

        record = self.store.create_job(
            job_id=job_id,
            session_id=request.session_id,
            cwd=request.cwd,
            request=_serialise_request(request),
            backend=backend_name,
        )

        cancel_event = threading.Event()
        thread = threading.Thread(
            target=self._run_job,
            args=(record.job_id, request, adapter, route_warnings, cancel_event),
            name=f"supervisor-{record.job_id}",
            daemon=True,
        )
        handle = _JobHandle(
            job_id=record.job_id,
            cancel_event=cancel_event,
            thread=thread,
        )
        with self._lock:
            self._jobs[record.job_id] = handle
        thread.start()

        return BridgeResponse(
            success=True,
            SESSION_ID=request.session_id or "",
            job_id=record.job_id,
            status="running",
            cwd=request.cwd,
            adapter=AdapterMetadata(
                backend=backend_name,
                bin_path=cap.bin_path or None,
                version=cap.version,
                model=request.model or cap.model,
                output_protocol=request.output_protocol,
                supports_streaming=cap.supports_streaming,
                supports_tool_events=cap.supports_tool_events,
            ),
            warnings=[*route_warnings, *cap.warnings],
        ).touch()

    def status(self, job_id: str) -> JobRecord | None:
        """Return the current :class:`JobRecord` for ``job_id`` or None.

        The record reflects whatever the worker thread has persisted so
        far; transient ``running`` -> ``completed`` transitions are
        observed eventually but never overlap.
        """

        record = self.store.get_job(job_id)
        if record is None:
            return None
        with self._lock:
            handle = self._jobs.get(job_id)
        # If the in-memory thread is gone but the on-disk record still
        # says ``running``, the worker must have crashed before finalising
        # — surface that as ``failed`` so callers don't poll forever.
        if record.status == "running" and (handle is None or not handle.thread.is_alive()):
            record = self.store.finalize_job(
                job_id, status="failed", error="worker thread exited without finalize",
            ) or record
        return record

    def read(
        self, job_id: str, *, since: int = 0, translate: str | None = None,
    ) -> list[CanonicalEvent] | list[dict]:
        """Return events from ``since`` onwards; optionally translate them."""

        events = self.store.read_events(job_id, since=since)
        if translate is None:
            return events
        translator = ProtocolTranslator(translate, safety=self.safety, include_raw=False)
        return translator.translate_many(events)

    def cancel(self, job_id: str) -> bool:
        """Signal a running job to stop; return True if a job was signalled."""

        with self._lock:
            handle = self._jobs.get(job_id)
        if handle is None:
            return False
        if not handle.thread.is_alive():
            return False
        handle.cancel_event.set()
        return True

    def list_sessions(self, *, limit: int | None = 50) -> list[JobRecord]:
        return self.store.list_jobs(limit=limit)

    # ------------------------------------------------------------------
    # Worker thread body
    # ------------------------------------------------------------------

    def _run_job(
        self,
        job_id: str,
        request: BridgeRequest,
        adapter: BaseAdapter,
        route_warnings: list[str],
        cancel_event: threading.Event,
    ) -> None:
        paths = JobPaths.for_job(self.store.root, job_id)
        sink = StoreEventSink(self.store, job_id)
        result: AdapterRunResult | None = None
        run_error: str | None = None
        spool_dir: Path | None = None
        try:
            cap = adapter.detect()
            # Spool dir lives for the lifetime of the run. We pre-allocate
            # paths under it so the adapter can write spool files; the dir
            # is removed in the ``finally`` once finalise has updated
            # JobRecord.{stdout,stderr,log}_path to point at the kept
            # copies in the session store.
            with tempfile.TemporaryDirectory(prefix="agy-mcp-sup-") as spool_root:
                spool_dir = Path(spool_root)
                spool_log = spool_dir / "agy.log" if cap.supports_log_file else None
                spool_stdout = spool_dir / "stdout.spool"
                spool_stderr = spool_dir / "stderr.spool"
                with self._lock:
                    handle = self._jobs.get(job_id)
                    if handle is not None:
                        handle.spool_dir = spool_dir
                try:
                    result = adapter.run(
                        request,
                        log_path=spool_log,
                        stdout_path=spool_stdout,
                        stderr_path=spool_stderr,
                        event_sink=sink,
                        cancel_event=cancel_event,
                    )
                except Exception as exc:  # noqa: BLE001 - keep finalize reachable
                    # Mirror bridge._run_unsafe: redact + cap the traceback
                    # so the job envelope never leaks a frame from the
                    # adapter's internals.
                    tb = self.safety.redact("".join(traceback.format_exception(exc)))[:4000]
                    run_error = self.safety.redact(str(exc)) + (
                        " | tb=" + tb if request.debug else ""
                    )
                else:
                    # Copy the spool stdout / stderr into the kept location
                    # before TemporaryDirectory deletes them. agy.log is
                    # also salvaged so post-mortem klog inspection works.
                    _migrate_if_present(spool_stdout, paths.stdout)
                    _migrate_if_present(spool_stderr, paths.stderr)
                    if spool_log is not None:
                        _migrate_if_present(spool_log, paths.agy_log)
        finally:
            self._finalize(
                job_id=job_id,
                result=result,
                run_error=run_error,
                cancel_event=cancel_event,
                request=request,
                route_warnings=route_warnings,
            )
            with self._lock:
                # Drop the in-memory handle so cancel() on a finished job
                # returns False and the next start() with the same id
                # can re-register cleanly.
                self._jobs.pop(job_id, None)

    def _finalize(
        self,
        *,
        job_id: str,
        result: AdapterRunResult | None,
        run_error: str | None,
        cancel_event: threading.Event,
        request: BridgeRequest,
        route_warnings: list[str],
    ) -> None:
        status: JobStatus
        exit_code: int | None = None
        error: str | None = run_error
        session_id_resolved: str | None = request.session_id

        if result is None:
            status = "cancelled" if cancel_event.is_set() else "failed"
        else:
            session_id_resolved = result.session_id or request.session_id
            exit_code = result.exit_code
            if cancel_event.is_set():
                status = "cancelled"
            elif result.exit_code == 0:
                status = "completed"
            else:
                status = "failed"
                if not error:
                    error = _pick_error_from_events(result.events) or "non-zero exit"

        finalised = self.store.finalize_job(
            job_id,
            status=status,
            exit_code=exit_code,
            session_id=session_id_resolved,
            error=error,
        )
        if finalised is None:
            return
        if result is not None and result.artifacts:
            finalised.artifacts = list(result.artifacts)
            self.store.update_job(finalised)
        # Stash the warnings list so MCP tools can surface it even after
        # the adapter has gone away. Store under ``extra`` so the
        # JobRecord schema stays stable.
        if route_warnings:
            finalised.extra.setdefault("route_warnings", list(route_warnings))
            self.store.update_job(finalised)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _migrate_if_present(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst`` if ``src`` exists; never raises.

    Called as the spool TemporaryDirectory is about to be removed —
    losing the file is acceptable (best-effort post-mortem), so we
    swallow OSError. The dst parent already exists because SessionStore
    created the job dir.
    """

    if not src.is_file():
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        # os.replace is atomic on POSIX and behaves correctly across the
        # same filesystem (spool dir is under /tmp; job dir typically
        # under ~/.agy-mcp/sessions). Cross-FS falls back to copy+unlink.
        os.replace(src, dst)
    except OSError:
        try:
            data = src.read_bytes()
            dst.write_bytes(data)
        except OSError:
            pass


def _serialise_request(request: BridgeRequest) -> dict:
    """Return a JSON-safe snapshot of the request for the JobRecord.

    Dumps via pydantic so any future field changes are reflected
    automatically; ``exclude_none=False`` keeps the snapshot stable.
    """

    return request.model_dump(exclude_none=False)


def _pick_error_from_events(events: list[CanonicalEvent]) -> str | None:
    for event in reversed(events):
        if event.type == "error" and event.text:
            return event.text
        if event.type == "result" and event.subtype not in ("success",) and event.text:
            return event.text
    return None


__all__ = [
    "AdapterFactory",
    "StoreEventSink",
    "Supervisor",
]
