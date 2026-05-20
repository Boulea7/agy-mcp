"""Adapter base classes: BaseAdapter contract, capability-cache helpers."""

from __future__ import annotations

import abc
import errno
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from agy_mcp.models import (
    BackendName,
    BridgeRequest,
    CanonicalEvent,
    Capability,
)
from agy_mcp.safety import SafetyPolicy
from agy_mcp.utils import resolve_executable


@dataclass(slots=True)
class AdapterRunResult:
    """Outcome of a single adapter.run invocation, before protocol translation."""

    events: list[CanonicalEvent]
    session_id: str | None
    exit_code: int | None
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    log_path: str | None
    artifacts: list[dict]


# ---------------------------------------------------------------------------
# Capability help-text parsing
# ---------------------------------------------------------------------------

# Capture any long/short flag token preceded by a non-identifier char.
# Using a negative lookbehind on alphanumeric/underscore so we don't pick
# up the trailing fragment of identifiers like ``foo-bar``. The MULTILINE
# anchor is no longer needed because ``-`` boundaries are explicit.
_FLAG_PATTERN = re.compile(r"(?<![\w.])(-{1,2}[A-Za-z][\w-]*)")


def detect_flags(help_text: str) -> set[str]:
    """Return the set of long/short flag names present in ``help_text``."""

    return {match.group(1) for match in _FLAG_PATTERN.finditer(help_text)}


def has_flag(help_text: str, *names: str) -> bool:
    flags = detect_flags(help_text)
    return any(name in flags for name in names)


# ---------------------------------------------------------------------------
# Run context — shared state across adapter reader threads.
# Lifted out of agy.py so GeminiCliBackend can reuse it without importing
# adapter internals across modules.
# ---------------------------------------------------------------------------

# Cap any single line read from klog / stream-json / drain pipes — a malicious
# or corrupt upstream could write a multi-GB line with no newline, blocking
# ``readline()`` indefinitely and growing memory unbounded.
_MAX_LINE_BYTES = 64 * 1024


@dataclass(slots=True)
class _RunContext:
    """Per-invocation state shared between the adapter reader threads.

    All mutation goes through ``lock`` so list append / set add /
    seen_session_id slot writes are deterministic even if a future runtime
    relaxes the GIL guarantees that make individual ops atomic today.
    """

    stdout_buf: list[str]
    stderr_buf: list[str]
    events: list[CanonicalEvent]
    seen_session_id: list[str | None]
    stop_event: threading.Event
    sink: "EventSink | None"
    transcript_seen: set[Path]
    lock: threading.Lock = field(default_factory=threading.Lock)


def _open_spool(path: Path):
    """Open ``path`` for append with O_NOFOLLOW when available.

    We distinguish two failure modes:

    * ``ELOOP`` — the path is already a symlink. Refuse to open at all
      (refusing fail-closed is the whole point of O_NOFOLLOW). An attacker
      who can pre-create the spool path as ``ln -s ~/.ssh/authorized_keys
      ./stdout.log`` would otherwise see adapter output appended to the
      symlink target.
    * ``ENOTSUP`` / ``EOPNOTSUPP`` / ``EINVAL`` — the filesystem rejects
      ``O_NOFOLLOW`` (rare; some NFS / FUSE mounts). Fall back to a plain
      append after one more ``is_symlink`` check so we still refuse the
      symlink even on those filesystems.
    """

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    nofollow_supported = hasattr(os, "O_NOFOLLOW")
    if nofollow_supported:
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # Path is a symlink — never fall back, always refuse.
            raise
        # Filesystem rejected the flag: fall back to a plain append after
        # one more symlink check so we still refuse a symlinked target.
        if not nofollow_supported or exc.errno in (
            errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL,
        ):
            if path.is_symlink():
                raise OSError(errno.ELOOP, f"refusing to follow symlink: {path}") from exc
            return path.open("a", encoding="utf-8")
        raise
    # Phase 4 R2 sec P3.1: close the raw fd explicitly if ``os.fdopen``
    # raises after the open — practically near-zero risk but the cleanup
    # cost is negligible.
    try:
        return os.fdopen(fd, "a", encoding="utf-8")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _drain_stream(
    stream,
    buf: list[str],
    ctx: _RunContext,
    spool_path: Path | None,
    label: str,
    adapter: "BaseAdapter | None" = None,
) -> None:
    """Copy stream content into ``buf`` and (optionally) to a spool file.

    When ``adapter`` is provided, spool-open refusals are surfaced through
    ``adapter.emit_event`` so the live sink (and the supervisor's session
    store behind it) sees the failure with proper lock + redaction. Older
    call sites that pass ``adapter=None`` get the in-memory event without
    sink fan-out (legacy compatibility for unit tests).
    """

    if stream is None:
        return
    spool = None
    if spool_path is not None:
        try:
            spool = _open_spool(spool_path)
        except OSError as exc:
            event = CanonicalEvent(
                type="error",
                subtype="spool_refused",
                text=f"refusing to open spool {label}: {exc}",
            )
            if adapter is not None:
                adapter.emit_event(ctx, event)
            else:
                with ctx.lock:
                    ctx.events.append(event)
    try:
        while not ctx.stop_event.is_set():
            chunk = stream.readline(_MAX_LINE_BYTES)
            if not chunk:
                break
            with ctx.lock:
                buf.append(chunk)
            if spool is not None:
                spool.write(chunk)
                spool.flush()
    except (OSError, ValueError):
        return
    finally:
        if spool is not None:
            try:
                spool.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# CanonicalEvent scrubbing — every adapter must run events through this
# before they reach the sink, so the supervisor's session store does not
# persist secrets.
# ---------------------------------------------------------------------------


def _scrub_event_in_place(event: CanonicalEvent, safety: SafetyPolicy) -> None:
    """Walk a CanonicalEvent and rewrite every string field through ``safety.redact``.

    Fields walked: ``text``, ``content``, ``metadata``, ``raw`` and any
    pydantic ``model_extra`` keys (CanonicalEvent has ``extra='allow'``).

    Fields intentionally skipped: ``session_id`` / ``role`` / ``subtype`` /
    ``type`` — these are regex-constrained (UUID-shaped session ids, fixed
    role enum, structural subtype labels) and redacting them would lose
    information the supervisor needs to route events.
    """

    if event.text:
        event.text = safety.redact(event.text)
    if event.content:
        event.content = [_scrub_mapping(item, safety) for item in event.content]
    if event.metadata:
        event.metadata = _scrub_mapping(event.metadata, safety)
    if event.raw:
        event.raw = _scrub_mapping(event.raw, safety)
    extra = getattr(event, "__pydantic_extra__", None)
    if extra:
        for key in list(extra.keys()):
            extra[key] = _scrub_mapping(extra[key], safety)


def _scrub_mapping(value, safety: SafetyPolicy, _depth: int = 0):
    """Recursively redact strings inside ``value``, capping recursion depth."""

    if _depth > 32:
        # Truncate rather than echoing the raw subtree, so secrets at depth
        # >32 never escape the redaction net.
        return {"__truncated__": True}
    if isinstance(value, str):
        return safety.redact(value)
    if isinstance(value, dict):
        return {k: _scrub_mapping(v, safety, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_mapping(v, safety, _depth + 1) for v in value]
    return value


# ---------------------------------------------------------------------------
# CWD hardening — adapters refuse to run inside a non-existent directory or
# one that resolves through a dangling symlink. Workspace-allowlist policy
# is bridge / supervisor territory (Phase 3+); the adapter just enforces
# the minimum invariant that ``cwd`` is a real local directory.
# ---------------------------------------------------------------------------


def resolve_cwd(cwd: str) -> Path:
    """Resolve ``cwd`` and verify it points at an existing directory.

    Raises ``RuntimeError`` if the path does not exist, resolves through a
    broken symlink, or points at something other than a directory.
    """

    try:
        resolved = Path(cwd).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"cwd does not resolve to a real path: {cwd!r}: {exc}") from exc
    if not resolved.is_dir():
        raise RuntimeError(f"cwd is not a directory: {resolved!r}")
    return resolved


class BaseAdapter(abc.ABC):
    """Common contract for AgyPrintBackend and GeminiCliBackend.

    Adapters are responsible for:
        1. Probing the bound binary (``detect``).
        2. Building the argv (``build_command``).
        3. Running the process and turning its output (stdout, log file,
           sidecar transcript.jsonl, etc.) into a list of
           :class:`CanonicalEvent` instances (``run``).

    Protocol translation (canonical → Claude / Codex / raw) lives in
    ``ProtocolTranslator``, not in adapters.
    """

    backend: BackendName

    def __init__(
        self,
        *,
        bin_override: str | None = None,
        safety: SafetyPolicy | None = None,
    ) -> None:
        self.bin_override = bin_override
        self.safety = safety or SafetyPolicy()
        self._capability: Capability | None = None

    # ------------------------------------------------------------------
    # Capability detection
    # ------------------------------------------------------------------

    def detect(self, *, refresh: bool = False) -> Capability:
        if self._capability is None or refresh:
            self._capability = self._probe()
        return self._capability

    @abc.abstractmethod
    def _probe(self) -> Capability:
        """Run the actual probe (help / version / settings)."""

    # ------------------------------------------------------------------
    # Command construction & execution
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def build_command(self, request: BridgeRequest, *, log_path: Path | None) -> list[str]:
        """Construct the argv that will be passed to subprocess.Popen."""

    @abc.abstractmethod
    def run(
        self,
        request: BridgeRequest,
        *,
        log_path: Path | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        event_sink: "EventSink | None" = None,
        cancel_event: "threading.Event | None" = None,
    ) -> "AdapterRunResult":
        """Run the bound CLI for ``request`` and return canonical events.

        ``cancel_event``: when supplied by the supervisor, the adapter's
        main wait loop polls it alongside ``proc.poll()`` and triggers the
        same terminate -> wait -> kill cascade as a wrapper timeout when
        the event is set. Reason field in the synthesised error is
        ``"cancelled"`` so callers can distinguish operator cancellation
        from a timeout.
        """

    # ------------------------------------------------------------------
    # Helpers shared by subclasses
    # ------------------------------------------------------------------

    def locate_binary(self, default_name: str) -> str | None:
        candidate = self.bin_override or shutil_which_or_none(default_name)
        if candidate:
            resolved = resolve_executable(candidate)
            if resolved:
                return resolved
        return None

    def emit_event(self, ctx: _RunContext, event: CanonicalEvent) -> None:
        """Scrub, append, and forward an event to the sink under the run lock."""

        _scrub_event_in_place(event, self.safety)
        with ctx.lock:
            ctx.events.append(event)
        if ctx.sink is not None:
            try:
                ctx.sink.emit(event)
            except Exception:  # noqa: BLE001 - sink errors must not poison the run
                pass


def shutil_which_or_none(name: str) -> str | None:
    return shutil.which(name)


# ---------------------------------------------------------------------------
# Process-group helpers — shared by every adapter that spawns a child whose
# subtree must die together (agy spawns a grpc sidecar; gemini-cli spawns a
# tool runner). Lifted out of agy.py so a third adapter (codex, copilot…)
# doesn't need a sibling import (Phase 4 R1 P2#8).
# ---------------------------------------------------------------------------


def _process_group_kwargs() -> dict:
    """Return the Popen kwarg that puts the child into its own group.

    On POSIX, ``start_new_session=True`` calls ``setsid`` in the child so
    the entire subtree (agy + grpc sidecar + language server) shares one
    process group. On Windows, ``CREATE_NEW_PROCESS_GROUP`` plays the same
    role for ``CTRL_BREAK_EVENT`` delivery. Passing both is harmless on
    POSIX but errors on Windows, so we branch.
    """

    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def _terminate_group(proc: "subprocess.Popen") -> None:
    """Send the polite shutdown signal to the entire process group.

    Falls back to ``proc.terminate()`` if we can't reach the group (e.g.
    the child already exited and the pgid lookup raises). Never raises.

    The ``proc.pid is None or proc.poll() is not None`` guard avoids PID
    recycling: once the kernel has reaped the original child, the same PID
    may belong to an unrelated process and we refuse to signal it.
    """

    if proc.pid is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            return
        except OSError:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.terminate()
    except OSError:
        pass


def _kill_group(proc: "subprocess.Popen") -> None:
    """Hard-kill the whole group; final stage of the terminate cascade."""

    if proc.pid is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        # No SIGKILL on Windows — TerminateProcess via proc.kill() will
        # only target the leader, but at this point any sidecar that
        # survived CTRL_BREAK_EVENT is probably stuck holding our pipes.
        # See docs/review-followups.md "windows-process-tree-cleanup".
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def wait_with_cancel_poll(
    proc: "subprocess.Popen",
    *,
    total_timeout: float,
    cancel_event: "threading.Event | None" = None,
    poll_interval: float = 0.5,
) -> int | None:
    """Wait up to ``total_timeout`` seconds for ``proc`` to exit.

    Polls in ``poll_interval`` steps so that a second cancel (or a wrapper
    deadline expiry) fired during the terminate grace window is honoured
    promptly rather than after the entire grace elapses. Returns the
    process exit code, or ``None`` if it never exited. Never raises.
    """

    if total_timeout <= 0:
        try:
            return proc.wait(timeout=0)
        except subprocess.TimeoutExpired:
            return None
    deadline = _monotonic() + total_timeout
    while True:
        remaining = deadline - _monotonic()
        if remaining <= 0:
            return None
        step = min(poll_interval, max(0.05, remaining))
        try:
            return proc.wait(timeout=step)
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                # Caller can now escalate (e.g. SIGKILL) without paying the
                # rest of the polite grace.
                return None


def _shutdown_cascade(
    proc: "subprocess.Popen",
    *,
    escalation_cancel_event: "threading.Event | None" = None,
    terminate_grace: float = 10.0,
    kill_grace: float = 5.0,
) -> int | None:
    """Run terminate -> wait -> kill on the child's whole process group.

    Each ``wait`` is broken into short polling steps via
    :func:`wait_with_cancel_poll`, so a second cancel arriving during the
    polite grace can shorten the wait rather than blocking the whole loop.
    Returns the child's exit code, or ``None`` if it survived both phases.

    ``escalation_cancel_event``: an OPTIONAL secondary cancel signal that
    can shortcut the polite grace (useful when the operator hits cancel a
    second time and wants the SIGKILL right now). Adapters today call
    this function FROM their own cancel/timeout branch, so they pass
    ``None`` — the first cancel already started this cascade and polling
    the same event would be redundant. A future "double-cancel = immediate
    SIGKILL" UX (tracked in followups.md) is the reason the parameter
    exists at all.

    Replaces the inline ``terminate -> wait(10) -> kill -> wait(5)`` blocks
    that previously lived in the adapter wait loops (Phase 4 R1 P1#7).
    """

    _terminate_group(proc)
    exit_code = wait_with_cancel_poll(
        proc,
        total_timeout=terminate_grace,
        cancel_event=escalation_cancel_event,
    )
    if exit_code is not None:
        return exit_code
    _kill_group(proc)
    return wait_with_cancel_poll(
        proc,
        total_timeout=kill_grace,
        cancel_event=escalation_cancel_event,
    )


def _monotonic() -> float:
    # Indirection so tests can monkeypatch without touching the stdlib.
    import time as _time

    return _time.monotonic()



# ---------------------------------------------------------------------------
# EventSink — pluggable hook used by Supervisor to forward live events to the
# session store / MCP progress notifications without coupling adapters to
# either.
# ---------------------------------------------------------------------------


class EventSink:
    """Simple sink: subclass and override ``emit`` to consume CanonicalEvents."""

    def emit(self, event: CanonicalEvent) -> None:  # pragma: no cover - default no-op
        return


class ListEventSink(EventSink):
    """Collect events into an in-memory list (testing aid)."""

    def __init__(self) -> None:
        self.events: list[CanonicalEvent] = []

    def emit(self, event: CanonicalEvent) -> None:
        self.events.append(event)


__all__ = [
    "AdapterRunResult",
    "BaseAdapter",
    "EventSink",
    "ListEventSink",
    "_MAX_LINE_BYTES",
    "_RunContext",
    "_drain_stream",
    "_kill_group",
    "_open_spool",
    "_process_group_kwargs",
    "_scrub_event_in_place",
    "_scrub_mapping",
    "_shutdown_cascade",
    "_terminate_group",
    "detect_flags",
    "has_flag",
    "resolve_cwd",
    "shutil_which_or_none",
    "wait_with_cancel_poll",
]
