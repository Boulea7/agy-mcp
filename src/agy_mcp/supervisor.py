"""Supervisor — async job manager that backs ``--detach`` and the MCP long-task tools.

Responsibilities:

1. ``start`` a job: spawn ``adapter.run`` on a worker thread; persist
   the :class:`JobRecord` immediately so the caller can poll while the
   adapter is still running.
2. ``status`` / ``read_events`` / ``read_translated`` / ``cancel`` /
   ``list_sessions``: read-only or process-controlling operations against
   the on-disk :class:`SessionStore` and the in-memory job registry.
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
* The supervisor MUST be the single writer per ``job_id``. Cross-process
  serialisation is deferred to Phase 4+ work (see
  ``docs/review-followups.md`` "cross-process slug collision").
"""

from __future__ import annotations

import os
import shutil
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
                # Disk full / permission revoked / unmounted / refused
                # symlink — eat the failure silently so a poisoned sink
                # cannot crash the adapter run. We deliberately do NOT
                # synthesise a "sink_write_failed" event here: the only
                # path that would persist it is the same sink that just
                # failed (Phase 4 R1 P1#6). Operators see the broken
                # store via the job dir on disk; programmatic visibility
                # is tracked as a Phase 4+ followup.
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


# Constant string used by the crash-reconcile path. Pulled out so tests
# can assert on it without hard-coding the literal across the codebase.
_RECONCILE_ERROR = "worker thread exited without finalize"


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
        max_concurrent_jobs: int = 8,
    ) -> None:
        self.store = store
        self.config = config or get_config()
        self.safety = safety or SafetyPolicy.from_config(self.config)
        # Default adapter factory routes via the bridge selector so the
        # supervisor and the synchronous path stay in lockstep.
        self._adapter_factory = adapter_factory or self._default_adapter_factory
        self._jobs: dict[str, _JobHandle] = {}
        self._lock = threading.RLock()
        # Cap concurrent worker threads so a flood of ``agy_start`` calls
        # can't spin up an unbounded number of subprocesses + reader
        # threads. (Phase 5 R2 security P1-3.)
        if max_concurrent_jobs <= 0:
            raise ValueError("max_concurrent_jobs must be positive")
        self._job_slots = threading.Semaphore(max_concurrent_jobs)
        self._max_concurrent_jobs = max_concurrent_jobs

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
        binary, duplicate job_id) produce ``success=False`` synchronously.
        Failures DURING the adapter run are visible only via ``status``
        / ``read_events``.
        """

        adapter, route_warnings = self._adapter_factory(
            request, self.config, self.safety,
        )
        cap = adapter.detect()
        backend_name = cap.backend

        # Redact every string that can reach the BridgeResponse — the
        # bridge contract requires this and the sync path at
        # ``bridge._run_unsafe`` already does it. Phase 4 R1 P1.1.
        cap_warnings = [self.safety.redact(w) for w in cap.warnings]
        route_warnings_redacted = [self.safety.redact(w) for w in route_warnings]

        if not cap.bin_path:
            error_text = (
                " | ".join(route_warnings_redacted)
                or self.safety.redact(f"backend={backend_name!r} unavailable")
            )
            return BridgeResponse(
                success=False,
                error=error_text,
                warnings=cap_warnings,
                cwd=request.cwd,
                adapter=AdapterMetadata(backend=backend_name),
            ).touch()

        # Reserve a concurrency slot before we even touch the session
        # store, so a slot rejection doesn't leak a half-populated record.
        # ``acquire(blocking=False)`` gives the caller a clean
        # ``success=False, error="server busy"`` envelope rather than
        # blocking the MCP tool call. (Phase 5 R2 security P1-3.)
        if not self._job_slots.acquire(blocking=False):
            return BridgeResponse(
                success=False,
                error=self.safety.redact(
                    f"supervisor busy: {self._max_concurrent_jobs} concurrent "
                    "jobs already running; retry after one finishes",
                ),
                warnings=[*route_warnings_redacted, *cap_warnings],
                cwd=request.cwd,
                adapter=AdapterMetadata(backend=backend_name),
            ).touch()

        try:
            record = self.store.create_job(
                job_id=job_id,
                session_id=request.session_id,
                cwd=request.cwd,
                request=_serialise_request(request),
                backend=backend_name,
            )
        except (FileExistsError, ValueError) as exc:
            self._job_slots.release()  # release the slot we just took
            return BridgeResponse(
                success=False,
                error=self.safety.redact(str(exc)),
                warnings=[*route_warnings_redacted, *cap_warnings],
                cwd=request.cwd,
                adapter=AdapterMetadata(backend=backend_name),
            ).touch()

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
            warnings=[*route_warnings_redacted, *cap_warnings],
        ).touch()

    def status(self, job_id: str) -> JobRecord | None:
        """Return the current :class:`JobRecord` for ``job_id`` or None.

        If the in-memory worker has already exited and dropped its handle
        but the on-disk record still says ``running`` (e.g. the worker
        process was SIGKILL'd before it reached ``_finalize``), we rewrite
        the record to ``failed``. The reconciliation is done under
        ``self._lock`` with a re-read of the meta file so a worker that
        races between persisting ``status=completed`` and popping its
        handle is NOT silently flipped back to failed (Phase 4 R1 P0#1).
        """

        record = self.store.get_job(job_id)
        if record is None:
            return None
        if record.status != "running":
            return record
        with self._lock:
            handle = self._jobs.get(job_id)
            handle_alive = handle is not None and handle.thread.is_alive()
            if handle_alive:
                # Still under management — no reconciliation needed.
                return record
            # Re-read inside the lock so a worker that just persisted
            # ``status=completed`` and is about to pop its handle wins
            # the race instead of being reclassified as failed.
            fresh = self.store.get_job(job_id)
            if fresh is None:
                return record
            if fresh.status != "running":
                return fresh
            finalised = self.store.finalize_job(
                job_id,
                status="failed",
                error=self.safety.redact(_RECONCILE_ERROR),
            )
            return finalised or fresh

    def read_events(self, job_id: str, *, since: int = 0) -> list[CanonicalEvent]:
        """Return canonical events from offset ``since`` onwards."""

        return self.store.read_events(job_id, since=since)

    def read_translated(
        self, job_id: str, *, since: int = 0, protocol: str = "claude",
    ) -> list[dict]:
        """Return events translated to the requested wire protocol.

        ``protocol`` matches ``ProtocolTranslator``'s ``protocol`` arg —
        ``raw`` / ``claude`` / ``codex``.
        """

        events = self.store.read_events(job_id, since=since)
        translator = ProtocolTranslator(protocol, safety=self.safety, include_raw=False)
        return translator.translate_many(events)

    def cancel(self, job_id: str) -> bool:
        """Signal a running job to stop; return True if a job was signalled.

        The read-check-set sequence is performed entirely under
        ``self._lock`` so a worker that exits between ``get`` and
        ``is_alive`` cannot have its slot reused by a fresh ``start()``
        before we set the wrong cancel_event (Phase 4 R1 P2.1).
        """

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
        try:
            cap = adapter.detect()
            # Spool dir lives for the lifetime of the run. We pre-allocate
            # paths under it so the adapter can write spool files; the dir
            # is removed in the ``finally`` once finalise has updated
            # JobRecord.{stdout,stderr,log}_path to point at the kept
            # copies in the session store.
            try:
                spool_ctx = tempfile.TemporaryDirectory(prefix="agy-mcp-sup-")
            except OSError as exc:
                # /tmp full / read-only — record the diagnostic so
                # status() doesn't show a bare ``failed`` (Phase 4 R1 P3.1).
                run_error = self.safety.redact(f"spool dir creation failed: {exc}")
            else:
                with spool_ctx as spool_root:
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
                        # Mirror bridge._run_unsafe: redact + cap the
                        # traceback so the job envelope never leaks a
                        # frame from the adapter's internals.
                        tb = self.safety.redact(
                            "".join(traceback.format_exception(exc)),
                        )[:4000]
                        run_error = self.safety.redact(str(exc)) + (
                            " | tb=" + tb if request.debug else ""
                        )
                    else:
                        # Copy the spool stdout / stderr into the kept
                        # location before TemporaryDirectory deletes
                        # them. agy.log is also salvaged so post-mortem
                        # klog inspection works.
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
            # Release the concurrency slot the start() path acquired so
            # the next queued job can begin. (Phase 5 R2 security P1-3.)
            self._job_slots.release()

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
        # Snapshot the cancel flag once so a *late* cancel firing after
        # the adapter cleanly returned does NOT silently downgrade a
        # successful run to ``cancelled`` (Phase 4 R1 P1#3).
        was_cancelled = cancel_event.is_set()

        if result is None:
            status = "cancelled" if was_cancelled else "failed"
        else:
            session_id_resolved = result.session_id or request.session_id
            exit_code = result.exit_code
            if result.exit_code == 0:
                # A clean exit always wins over a late cancel. Cancel that
                # arrived while the adapter was still inside its wait
                # loop will already have set exit_code != 0 via the
                # terminate cascade, so this branch keeps that flow.
                status = "completed"
            elif was_cancelled:
                status = "cancelled"
            else:
                status = "failed"
                if not error:
                    error = _pick_error_from_events(result.events) or "non-zero exit"

        # Atomic single-write finalize: mutate the record in memory and
        # call ``update_job`` exactly once so a reader cannot observe a
        # ``status=completed`` record without its artifacts / route
        # warnings (Phase 4 R1 P1#5).
        record = self.store.get_job(job_id)
        if record is None:
            # Phase 4 R2 P2.2: the meta file disappeared mid-run (e.g. a
            # concurrent ``retention.purge_older_than`` raced us, or the
            # operator deleted it manually). Without a re-materialised
            # record, ``status(job_id)`` would return ``None`` forever
            # and the terminal status would be silently lost. Emit a
            # diagnostic to the event log so post-mortem inspection is
            # possible, then bail — re-creating the meta would conflict
            # with the FileExistsError invariant added in R1 P1.2.
            try:
                self.store.append_event(
                    job_id,
                    CanonicalEvent(
                        type="error",
                        subtype="meta_lost",
                        text=self.safety.redact(
                            f"job meta vanished before finalize: status={status!r}",
                        ),
                    ),
                )
            except OSError:
                # Event log might also be gone — nothing more we can do.
                pass
            return
        record.status = status
        record.exit_code = exit_code
        record.finished_at = _utc_now()
        if session_id_resolved and not record.session_id:
            record.session_id = session_id_resolved
        if error is not None:
            record.error = error
        if result is not None and result.artifacts:
            record.artifacts = list(result.artifacts)
        if route_warnings:
            record.extra.setdefault("route_warnings", list(route_warnings))
        self.store.update_job(record)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    # Indirection so finalize uses the same clock helper as the rest of
    # the codebase (utils.utc_now_iso). Imported lazily to keep the
    # supervisor's module-level import surface narrow.
    from agy_mcp.utils import utc_now_iso

    return utc_now_iso()


def _migrate_if_present(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst`` if ``src`` exists; never raises.

    Streams the body via :func:`shutil.copyfile` on cross-FS fallback so
    a multi-hundred-MB spool stdout does not balloon worker RAM (Phase 4
    R1 P1#4). The destination is opened with ``O_WRONLY|O_NOFOLLOW``
    when available so a symlink planted in the job dir cannot redirect
    the migrate target (Phase 4 R1 P2.3).
    """

    if not src.is_file():
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    # If the destination already exists as a symlink, refuse to migrate
    # — the attacker could have planted ``stdout.log -> ~/.ssh/authorized_keys``
    # between create_job and the migrate call.
    try:
        if dst.is_symlink():
            return
    except OSError:
        return
    try:
        os.replace(src, dst)
        _try_chmod(dst, 0o600)
        return
    except OSError:
        # os.replace fails across filesystems; fall back to streaming
        # copy. ``shutil.copyfile`` opens dst with ``O_WRONLY|O_CREAT|
        # O_TRUNC``; we re-check the symlink invariant first to keep the
        # window between the dst.is_symlink check above and the open
        # call as tight as we can without dropping to ctypes.
        pass
    try:
        _safe_copyfile(src, dst)
        _try_chmod(dst, 0o600)
        try:
            src.unlink()
        except OSError:
            pass
    except OSError:
        return


def _safe_copyfile(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` with O_NOFOLLOW on the destination.

    Uses a manual chunked read/write loop on top of ``os.open`` rather
    than ``shutil.copyfile`` so the destination flags include
    ``O_NOFOLLOW`` (when supported) — copyfile internally calls
    ``open(dst, 'wb')`` which lacks that protection.

    On failure, attempts to unlink the (possibly truncated) destination
    so the job dir does not retain a half-written file (Phase 4 R2 sec
    P3.2).
    """

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(dst, flags, 0o600)
    try:
        dst_fp = os.fdopen(fd, "wb")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    try:
        with dst_fp, src.open("rb") as src_fp:
            shutil.copyfileobj(src_fp, dst_fp, length=64 * 1024)
    except BaseException:
        # Best-effort cleanup of the truncated partial file.
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise


def _try_chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
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
