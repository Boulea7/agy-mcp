"""AgyPrintBackend — wraps Google Antigravity CLI (`agy --print`).

Strategy (see docs/output-strategy.md for the full rationale):

1. Spawn ``agy --print <prompt> --print-timeout <dur> --log-file <tmp>
   [--conversation <id> | --continue] [--sandbox]`` with stdout/stderr piped.
2. Three concurrent readers:
   - **stdout**: agy prints the final assistant text once at the end (no
     token streaming). Buffer it; emit one ``assistant/text`` event when
     the process exits.
   - **klog tail of --log-file**: emit lifecycle events on the fly.
   - **transcript.jsonl watcher** (optional, best-effort): pass through
     any subagent NDJSON the CLI writes.
3. On exit, emit ``result/success`` or ``result/error`` with timing /
   exit code / extracted conversation_id.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agy_mcp.adapters.base import (
    AdapterRunResult,
    BaseAdapter,
    EventSink,
    has_flag,
)
from agy_mcp.models import BackendName, BridgeRequest, CanonicalEvent, Capability
from agy_mcp.safety import DEFAULT_SCRUB_ENV_NAMES, SafetyPolicy
from agy_mcp.utils import (
    is_windows,
    scrub_env,
    truncate_middle,
    utc_now_iso,
    windows_escape,
)

# ---------------------------------------------------------------------------
# Paths & probes
# ---------------------------------------------------------------------------

AGY_BINARY_NAME = "agy"
AGY_SETTINGS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
AGY_GEMINI_SETTINGS_PATH = Path.home() / ".gemini" / "settings.json"
AGY_OAUTH_CREDS_PATH = Path.home() / ".gemini" / "oauth_creds.json"
AGY_LOG_DIR = Path.home() / ".gemini" / "antigravity-cli" / "log"
AGY_HELP_TIMEOUT_S = 10
AGY_VERSION_TIMEOUT_S = 10
# Polling cadence for klog / transcript tails. Tuned for CLI responsiveness
# (klog flushes per line, so 50ms keeps us within a single human RTT) while
# keeping idle CPU near zero.
_TAIL_POLL_INTERVAL_S = 0.05


# ---------------------------------------------------------------------------
# klog line patterns (see docs/cli-capabilities.md for the full list).
# ---------------------------------------------------------------------------

_KLOG_LINE = re.compile(
    r"^[IWEF]\d{4}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+[\w./]+:\d+]\s+(?P<msg>.*)$"
)

_RE_GRPC_PORT = re.compile(r"Language server listening on random port at (\d+) for HTTPS")
_RE_HTTP_PORT = re.compile(r"Language server listening on random port at (\d+) for HTTP\b")
_RE_CREATED_CONV = re.compile(r"Created conversation ([0-9a-fA-F-]{8,})")
_RE_PRINT_START = re.compile(
    r'Print mode: starting \(promptLength=(\d+), model=(?:"([^"]*)")?(?:[^,]*), '
    r'conversationID=(?:"([^"]*)")?\)'
)
_RE_RESUMING_CONV = re.compile(r"Print mode: resuming conversation ([0-9a-fA-F-]{8,})")
_RE_NEW_CONV = re.compile(r"Starting new conversation \(agent=(true|false)\)")
_RE_AUTO_FLUSH = re.compile(
    r"Auto-flush: sending (\d+) queued input\(s\) \(combinedLength=(\d+), media=(\d+)\)"
)
_RE_SEND_FAILED = re.compile(r"Print mode: SendUserMessage failed: (.+)")
_RE_AUTH_TIMEOUT = re.compile(r"Print mode: auth timed out")
_RE_AUTH_ERROR = re.compile(r"Print mode: auth error: (.+)")
_RE_REWIND = re.compile(r"Rewinding conversation [0-9a-fA-F-]+ to step (\d+)")
_RE_STREAM_START = re.compile(r"Starting conversation update stream for ([0-9a-fA-F-]+)")
_RE_TURN_END = re.compile(r"Stopping conversation stream|Language server shutting down")


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RunContext:
    """Per-invocation state shared between the three concurrent readers."""

    stdout_buf: list[str]
    stderr_buf: list[str]
    events: list[CanonicalEvent]
    seen_session_id: list[str | None]  # mutable slot
    stop_event: threading.Event
    sink: EventSink | None
    transcript_seen: set[Path]


class AgyPrintBackend(BaseAdapter):
    backend: BackendName = "agy"

    # ------------------------------------------------------------------
    # Capability detection
    # ------------------------------------------------------------------

    def _probe(self) -> Capability:
        bin_path = self.locate_binary(AGY_BINARY_NAME)
        cap = Capability(
            bin_path=bin_path or "",
            backend="agy",
            version=None,
            authenticated=AGY_OAUTH_CREDS_PATH.exists(),
            model=self._discover_model(),
            warnings=[],
        )
        if not bin_path:
            cap.warnings.append(
                f"{AGY_BINARY_NAME!r} not found on PATH; set AGY_BIN to override "
                "or install via https://antigravity.google/cli/install.sh"
            )
            return cap

        help_text = self._run_probe([bin_path, "--help"], timeout=AGY_HELP_TIMEOUT_S)
        version_text = self._run_probe([bin_path], extra=["--version"], timeout=AGY_VERSION_TIMEOUT_S)
        cap.raw_help = help_text or None
        cap.version = _parse_version(version_text) or _parse_version_from_help(help_text)

        text = help_text or ""
        cap.supports_print = has_flag(text, "--print", "-p", "--prompt")
        cap.supports_print_timeout = has_flag(text, "--print-timeout")
        cap.supports_conversation = has_flag(text, "--conversation")
        cap.supports_continue = has_flag(text, "--continue", "-c")
        cap.supports_sandbox = has_flag(text, "--sandbox")
        cap.supports_log_file = has_flag(text, "--log-file")
        cap.supports_add_dir = has_flag(text, "--add-dir")
        cap.supports_dangerously_skip_permissions = has_flag(
            text, "--dangerously-skip-permissions"
        )
        # agy v1.0.0 has no JSON / stream-json output today; surface explicitly.
        cap.supports_streaming = False
        cap.supports_tool_events = False

        if not cap.authenticated:
            cap.warnings.append(
                f"OAuth credentials missing at {AGY_OAUTH_CREDS_PATH}; "
                "`agy --print` will hang silently. Run `agy` once and log in."
            )
        if not cap.supports_print:
            cap.warnings.append(
                "`agy --print` not detected in --help; this build of agy may not "
                "support non-interactive mode."
            )
        return cap

    @staticmethod
    def _run_probe(cmd: list[str], *, timeout: int, extra: list[str] | None = None) -> str:
        """Best-effort subprocess probe; returns combined stdout+stderr or empty string."""

        try:
            proc = subprocess.run(  # noqa: S603 - argv is hard-coded probe
                cmd + (extra or []),
                capture_output=True,
                timeout=timeout,
                env=_scrub_probe_env(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        out = (proc.stdout or b"").decode("utf-8", errors="replace")
        err = (proc.stderr or b"").decode("utf-8", errors="replace")
        return out + ("\n" + err if err else "")

    @staticmethod
    def _discover_model() -> str | None:
        """Read the active model label from agy's settings file (read-only)."""

        for path in (AGY_SETTINGS_PATH, AGY_GEMINI_SETTINGS_PATH):
            try:
                if not path.is_file():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if isinstance(data.get("model"), str):
                return data["model"]
            model_section = data.get("model")
            if isinstance(model_section, dict) and isinstance(model_section.get("name"), str):
                return model_section["name"]
        return None

    # ------------------------------------------------------------------
    # Command construction
    # ------------------------------------------------------------------

    def build_command(self, request: BridgeRequest, *, log_path: Path | None) -> list[str]:
        cap = self.detect()
        if not cap.bin_path:
            raise RuntimeError(
                f"agy binary not found; pass --agy-bin or set AGY_BIN. {cap.warnings!r}"
            )
        if not cap.supports_print:
            raise RuntimeError(
                "Installed `agy` does not advertise --print; check `agy --help`."
            )
        argv: list[str] = [cap.bin_path, "--print", self._prepare_prompt(request.prompt)]

        if cap.supports_print_timeout:
            # Reserve wrapper-side grace for klog drain + child cleanup.
            inner_timeout = max(30, request.timeout - 30)
            argv += ["--print-timeout", f"{inner_timeout}s"]
        if cap.supports_log_file and log_path is not None:
            argv += ["--log-file", str(log_path)]
        if request.sandbox and cap.supports_sandbox:
            argv.append("--sandbox")
        if request.session_id:
            if cap.supports_conversation:
                argv += ["--conversation", request.session_id]
        elif cap.supports_continue and request.backend == "agy":
            # Only auto-continue when the caller explicitly chose the agy
            # backend and gave no session id; for auto/gemini routing the
            # supervisor should set session_id explicitly to avoid surprises.
            pass  # do not auto-add --continue; require explicit session_id
        return argv

    @staticmethod
    def _prepare_prompt(prompt: str) -> str:
        return windows_escape(prompt) if is_windows() else prompt

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(
        self,
        request: BridgeRequest,
        *,
        log_path: Path | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        event_sink: EventSink | None = None,
    ) -> AdapterRunResult:
        cap = self.detect()
        argv = self.build_command(request, log_path=log_path)

        ctx = _RunContext(
            stdout_buf=[],
            stderr_buf=[],
            events=[],
            seen_session_id=[request.session_id],
            stop_event=threading.Event(),
            sink=event_sink,
            transcript_seen=set(),
        )

        self._emit(ctx, _system_init_event(request=request, cap=cap))

        env = self._build_subprocess_env(request)
        start = time.time()
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv built from probed cap
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(Path(request.cwd).expanduser().resolve()),
                env=env,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self._emit(
                ctx,
                CanonicalEvent(
                    type="error",
                    subtype="spawn_failure",
                    text=f"failed to spawn {argv[0]!r}: {exc}",
                ),
            )
            duration = int((time.time() - start) * 1000)
            return AdapterRunResult(
                events=ctx.events,
                session_id=ctx.seen_session_id[0],
                exit_code=None,
                duration_ms=duration,
                stdout_tail="",
                stderr_tail=str(exc),
                log_path=str(log_path) if log_path else None,
                artifacts=[],
            )

        threads: list[threading.Thread] = []
        threads.append(
            threading.Thread(
                target=_drain_stream,
                args=(proc.stdout, ctx.stdout_buf, ctx, stdout_path, "stdout"),
                daemon=True,
            )
        )
        threads.append(
            threading.Thread(
                target=_drain_stream,
                args=(proc.stderr, ctx.stderr_buf, ctx, stderr_path, "stderr"),
                daemon=True,
            )
        )
        if log_path is not None:
            threads.append(
                threading.Thread(
                    target=_tail_klog,
                    args=(log_path, ctx, self),
                    daemon=True,
                )
            )
        threads.append(
            threading.Thread(
                target=_tail_transcripts,
                args=(ctx, self, start),
                daemon=True,
            )
        )
        for t in threads:
            t.start()

        deadline = start + max(request.timeout, 1)
        exit_code: int | None = None
        timed_out = False
        try:
            while True:
                if proc.poll() is not None:
                    exit_code = proc.returncode
                    break
                if time.time() >= deadline:
                    timed_out = True
                    self._emit(
                        ctx,
                        CanonicalEvent(
                            type="error",
                            subtype="wrapper_timeout",
                            text=f"agy did not finish within {request.timeout}s",
                        ),
                    )
                    proc.terminate()
                    try:
                        exit_code = proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            exit_code = proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            exit_code = None
                    break
                time.sleep(0.05)
        finally:
            ctx.stop_event.set()
            for t in threads:
                t.join(timeout=5)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

        stdout_text = "".join(ctx.stdout_buf)
        stderr_text = "".join(ctx.stderr_buf)

        if stdout_text.strip():
            self._emit(
                ctx,
                CanonicalEvent(
                    type="assistant",
                    subtype="text",
                    session_id=ctx.seen_session_id[0],
                    role="assistant",
                    text=stdout_text,
                    content=[{"type": "text", "text": stdout_text}],
                ),
            )

        duration_ms = int((time.time() - start) * 1000)
        if exit_code == 0 and not timed_out:
            self._emit(
                ctx,
                CanonicalEvent(
                    type="result",
                    subtype="success",
                    session_id=ctx.seen_session_id[0],
                    metadata={
                        "duration_ms": duration_ms,
                        "exit_code": exit_code,
                        "conversation_id": ctx.seen_session_id[0],
                    },
                ),
            )
        else:
            self._emit(
                ctx,
                CanonicalEvent(
                    type="result",
                    subtype="error" if not timed_out else "wrapper_timeout",
                    session_id=ctx.seen_session_id[0],
                    text=self.safety.redact(stderr_text)[:2000],
                    metadata={
                        "duration_ms": duration_ms,
                        "exit_code": exit_code,
                        "conversation_id": ctx.seen_session_id[0],
                        "timed_out": timed_out,
                    },
                ),
            )

        return AdapterRunResult(
            events=ctx.events,
            session_id=ctx.seen_session_id[0],
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_tail=truncate_middle(stdout_text, max_chars=request.max_output_chars),
            stderr_tail=truncate_middle(
                self.safety.redact(stderr_text), max_chars=request.max_output_chars
            ),
            log_path=str(log_path) if log_path else None,
            artifacts=[],
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit(self, ctx: _RunContext, event: CanonicalEvent) -> None:
        ctx.events.append(event)
        if ctx.sink is not None:
            try:
                ctx.sink.emit(event)
            except Exception:  # noqa: BLE001 - sink errors must not poison the run
                pass

    def _build_subprocess_env(self, request: BridgeRequest) -> dict[str, str]:
        env = dict(os.environ)
        for key in DEFAULT_SCRUB_ENV_NAMES:
            # We do NOT scrub here — `agy` needs its own OAuth file, not env
            # vars — but we must not export wrapper-side secret vars that the
            # subprocess does not need. Leave the host environment unchanged
            # except for explicit overrides.
            env.pop(key, None) if False else None
        if request.session_id:
            env["ANTIGRAVITY_CONVERSATION_ID"] = request.session_id
        env.setdefault("AGY_CLI_DISABLE_AUTO_UPDATE", "1")
        env.update(request.extra_env or {})
        return env


# ---------------------------------------------------------------------------
# Free functions used by adapter threads
# ---------------------------------------------------------------------------


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
    spool = spool_path.open("a", encoding="utf-8") if spool_path else None
    try:
        while not ctx.stop_event.is_set():
            chunk = stream.readline()
            if not chunk:
                # End of stream → wait for process exit to also stop tailing.
                break
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


def _tail_klog(log_path: Path, ctx: _RunContext, adapter: AgyPrintBackend) -> None:
    """Tail the --log-file written by agy and emit structured lifecycle events."""

    end = time.time() + 60.0  # wait up to 60s for the log file to appear
    fp = None
    try:
        while not ctx.stop_event.is_set():
            if fp is None:
                if log_path.exists():
                    try:
                        fp = log_path.open("r", encoding="utf-8", errors="replace")
                    except OSError:
                        time.sleep(_TAIL_POLL_INTERVAL_S)
                        continue
                elif time.time() > end:
                    return
                else:
                    time.sleep(_TAIL_POLL_INTERVAL_S)
                    continue
            line = fp.readline()
            if not line:
                if ctx.stop_event.is_set():
                    break
                time.sleep(_TAIL_POLL_INTERVAL_S)
                continue
            _handle_klog_line(line, ctx, adapter)
        # Final drain: even if stop_event fired before fp was opened (very
        # short-lived subprocess case), make one last attempt to read the
        # log so we don't lose lifecycle events like "Created conversation".
        if fp is None and log_path.exists():
            try:
                fp = log_path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                fp = None
        if fp is not None:
            while True:
                remainder = fp.readline()
                if not remainder:
                    break
                _handle_klog_line(remainder, ctx, adapter)
    finally:
        if fp is not None:
            try:
                fp.close()
            except OSError:
                pass


def _handle_klog_line(line: str, ctx: _RunContext, adapter: AgyPrintBackend) -> None:
    match = _KLOG_LINE.match(line)
    msg = (match.group("msg") if match else line).strip()
    if not msg:
        return

    if m := _RE_GRPC_PORT.search(msg):
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="sidecar_ready",
                metadata={"grpc_port": int(m.group(1)), "raw": msg},
            ),
        )
        return
    if m := _RE_HTTP_PORT.search(msg):
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="sidecar_http_ready",
                metadata={"http_port": int(m.group(1)), "raw": msg},
            ),
        )
        return
    if m := _RE_CREATED_CONV.search(msg):
        sid = m.group(1)
        ctx.seen_session_id[0] = sid
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="conversation_started",
                session_id=sid,
                metadata={"raw": msg},
            ),
        )
        return
    if m := _RE_RESUMING_CONV.search(msg):
        sid = m.group(1)
        ctx.seen_session_id[0] = sid
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="conversation_resumed",
                session_id=sid,
                metadata={"raw": msg},
            ),
        )
        return
    if m := _RE_PRINT_START.search(msg):
        prompt_len = int(m.group(1))
        model = m.group(2) or None
        sid = m.group(3) or None
        if sid:
            ctx.seen_session_id[0] = sid
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="print_starting",
                session_id=sid,
                metadata={"prompt_length": prompt_len, "model": model},
            ),
        )
        return
    if m := _RE_NEW_CONV.search(msg):
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="turn_start",
                metadata={"agent_mode": m.group(1) == "true"},
            ),
        )
        return
    if m := _RE_AUTO_FLUSH.search(msg):
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="user",
                subtype="input_flush",
                metadata={
                    "input_count": int(m.group(1)),
                    "combined_chars": int(m.group(2)),
                    "media": int(m.group(3)),
                },
            ),
        )
        return
    if m := _RE_SEND_FAILED.search(msg):
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="error",
                subtype="send_user_message_failed",
                text=adapter.safety.redact(m.group(1))[:1000],
            ),
        )
        return
    if _RE_AUTH_TIMEOUT.search(msg):
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="error",
                subtype="auth_timeout",
                text="Antigravity OAuth flow timed out; run `agy` once to authenticate.",
            ),
        )
        return
    if m := _RE_AUTH_ERROR.search(msg):
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="error",
                subtype="auth_error",
                text=adapter.safety.redact(m.group(1))[:500],
            ),
        )
        return
    if m := _RE_REWIND.search(msg):
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="rewind",
                metadata={"step": int(m.group(1))},
            ),
        )
        return
    if m := _RE_STREAM_START.search(msg):
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="stream_start",
                session_id=m.group(1),
            ),
        )
        return
    if _RE_TURN_END.search(msg):
        adapter._emit(
            ctx,
            CanonicalEvent(type="system", subtype="turn_end", metadata={"raw": msg}),
        )
        return


def _tail_transcripts(ctx: _RunContext, adapter: AgyPrintBackend, started_at: float) -> None:
    """Best-effort watcher for subagent transcript.jsonl files.

    agy writes these to a per-subagent dynamic path under ~/.gemini/antigravity-cli/log/.
    We only consider files created after the current invocation started so we
    do not replay history from prior sessions.
    """

    if not AGY_LOG_DIR.exists():
        return
    while not ctx.stop_event.is_set():
        try:
            for candidate in AGY_LOG_DIR.rglob("transcript.jsonl"):
                if candidate in ctx.transcript_seen:
                    continue
                try:
                    if candidate.stat().st_mtime < started_at - 1:
                        ctx.transcript_seen.add(candidate)
                        continue
                except OSError:
                    continue
                ctx.transcript_seen.add(candidate)
                _drain_transcript(candidate, ctx, adapter)
        except OSError:
            pass
        if ctx.stop_event.wait(timeout=0.5):
            return


def _drain_transcript(path: Path, ctx: _RunContext, adapter: AgyPrintBackend) -> None:
    """Pass-through every NDJSON line of a transcript.jsonl as subagent_event."""

    try:
        fp = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    try:
        while not ctx.stop_event.is_set():
            line = fp.readline()
            if not line:
                if ctx.stop_event.wait(timeout=_TAIL_POLL_INTERVAL_S):
                    return
                continue
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            adapter._emit(
                ctx,
                CanonicalEvent(
                    type="subagent_event",
                    subtype=str(payload.get("type") or "unknown"),
                    raw=payload if isinstance(payload, dict) else {"value": payload},
                    metadata={"transcript_path": str(path)},
                ),
            )
    finally:
        try:
            fp.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _system_init_event(*, request: BridgeRequest, cap: Capability) -> CanonicalEvent:
    return CanonicalEvent(
        type="system",
        subtype="init",
        session_id=request.session_id,
        metadata={
            "backend": "agy",
            "bin_path": cap.bin_path,
            "version": cap.version,
            "model": cap.model,
            "cwd": request.cwd,
            "mode": request.mode,
            "sandbox": request.sandbox,
            "capabilities": {
                "streaming": cap.supports_streaming,
                "tool_use": cap.supports_tool_events,
                "resume": cap.supports_conversation,
                "log_file": cap.supports_log_file,
                "sandbox": cap.supports_sandbox,
            },
            "authenticated": cap.authenticated,
            "warnings": list(cap.warnings),
            "ts": utc_now_iso(),
        },
    )


def _parse_version(output: str) -> str | None:
    if not output:
        return None
    for line in output.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"\d+\.\d+\.\d+(?:[+\-]\S+)?", stripped):
            return stripped
    return None


def _parse_version_from_help(help_text: str) -> str | None:
    if not help_text:
        return None
    m = re.search(r"version[:\s]+(\d+\.\d+\.\d+(?:[+\-]\S+)?)", help_text, re.IGNORECASE)
    return m.group(1) if m else None


def _scrub_probe_env() -> dict[str, str]:
    """Environment used only for capability probes; secrets stripped."""

    return scrub_env(dict(os.environ))


__all__ = [
    "AGY_BINARY_NAME",
    "AGY_OAUTH_CREDS_PATH",
    "AGY_SETTINGS_PATH",
    "AgyPrintBackend",
]
