"""Adapter base classes: BaseAdapter contract, capability-cache helpers."""

from __future__ import annotations

import abc
import os
import re
import shutil
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

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
    """Open ``path`` for append with O_NOFOLLOW when available."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        # Fall back to append-mode open; rare on POSIX, but tolerate
        # filesystems that reject O_NOFOLLOW (some network mounts).
        return path.open("a", encoding="utf-8")
    return os.fdopen(fd, "a", encoding="utf-8")


def _drain_stream(
    stream,
    buf: list[str],
    ctx: _RunContext,
    spool_path: Path | None,
    label: str,
) -> None:
    """Copy stream content into ``buf`` and (optionally) to a spool file."""

    if stream is None:
        return
    spool = _open_spool(spool_path) if spool_path else None
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
    """Walk a CanonicalEvent and rewrite every string field through ``safety.redact``."""

    if event.text:
        event.text = safety.redact(event.text)
    if event.content:
        event.content = [_scrub_mapping(item, safety) for item in event.content]
    if event.metadata:
        event.metadata = _scrub_mapping(event.metadata, safety)
    if event.raw:
        event.raw = _scrub_mapping(event.raw, safety)


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
    ) -> "AdapterRunResult":
        """Run the bound CLI for ``request`` and return canonical events."""

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
    "_open_spool",
    "_scrub_event_in_place",
    "_scrub_mapping",
    "detect_flags",
    "has_flag",
    "resolve_cwd",
    "shutil_which_or_none",
]
