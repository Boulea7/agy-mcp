"""Adapter base classes: BaseAdapter contract, capability-cache helpers."""

from __future__ import annotations

import abc
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
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
    ) -> AdapterRunResult:
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


def shutil_which_or_none(name: str) -> str | None:
    return shutil.which(name)


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
    "detect_flags",
    "has_flag",
    "shutil_which_or_none",
]
