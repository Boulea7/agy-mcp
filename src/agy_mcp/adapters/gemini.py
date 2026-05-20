"""GeminiCliBackend — fallback adapter that wraps `gemini --output-format stream-json`.

When `agy --print` cannot satisfy a request that needs real streaming or tool
events, the router falls back to `gemini-cli` (>= 0.42) which shares the same
Google OAuth backend and emits native NDJSON events. We parse those events
and re-emit them as CanonicalEvent instances.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from agy_mcp.adapters.base import (
    AdapterRunResult,
    BaseAdapter,
    EventSink,
    _MAX_LINE_BYTES,
    _RunContext,
    _drain_stream,
    _process_group_kwargs,
    _shutdown_cascade,
    has_flag,
    resolve_cwd,
)
# Process-group helpers moved to adapters/base in Phase 4 R1 P2#8 so
# every adapter that spawns a subtree can share the cancel cascade
# without sibling-private imports.
from agy_mcp.models import BackendName, BridgeRequest, CanonicalEvent, Capability
from agy_mcp.utils import (
    augment_path_env_for_windows,
    is_windows,
    prepare_subprocess_command,
    scrub_env,
    truncate_middle,
    utc_now_iso,
    windows_escape,
)

GEMINI_BINARY_NAME = "gemini"
GEMINI_HELP_TIMEOUT_S = 10
_TURN_COMPLETED_GRACE_S = 0.3
_MAX_STREAM_JSON_RECORD_BYTES = 1024 * 1024


class GeminiCliBackend(BaseAdapter):
    backend: BackendName = "gemini"

    def _probe(self) -> Capability:
        bin_path = self.locate_binary(GEMINI_BINARY_NAME)
        cap = Capability(
            bin_path=bin_path or "",
            backend="gemini",
            warnings=[],
        )
        if not bin_path:
            cap.warnings.append(
                f"{GEMINI_BINARY_NAME!r} not found on PATH; install gemini-cli or "
                "set GEMINI_BIN to use this fallback."
            )
            return cap
        help_text = self._run_probe([bin_path, "--help"], timeout=GEMINI_HELP_TIMEOUT_S)
        cap.raw_help = help_text or None
        cap.version = _parse_version(
            self._run_probe([bin_path, "--version"], timeout=GEMINI_HELP_TIMEOUT_S)
        )
        text = help_text or ""
        cap.supports_print = has_flag(text, "--prompt", "-p")
        cap.supports_print_timeout = False
        cap.supports_conversation = has_flag(text, "--resume")
        cap.supports_continue = False
        cap.supports_sandbox = has_flag(text, "--sandbox")
        cap.supports_log_file = False
        cap.supports_add_dir = False
        cap.supports_dangerously_skip_permissions = False
        cap.supports_streaming = "stream-json" in text or "stream_json" in text
        cap.supports_tool_events = cap.supports_streaming
        return cap

    @staticmethod
    def _run_probe(cmd: list[str], *, timeout: int) -> str:
        try:
            proc = subprocess.run(  # noqa: S603 - argv hard-coded
                cmd,
                capture_output=True,
                timeout=timeout,
                env=scrub_env(dict(os.environ)),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        out = (proc.stdout or b"").decode("utf-8", errors="replace")
        err = (proc.stderr or b"").decode("utf-8", errors="replace")
        return out + ("\n" + err if err else "")

    def build_command(self, request: BridgeRequest, *, log_path: Path | None) -> list[str]:
        cap = self.detect()
        if not cap.bin_path:
            raise RuntimeError("gemini binary not found; set GEMINI_BIN.")
        prompt = windows_escape(request.prompt) if is_windows() else request.prompt
        # H1 (Phase 3 review): fused ``--prompt=<value>`` keeps the prompt
        # inside a single argv element so a hostile prompt starting with
        # ``--`` cannot peel off into a fresh flag. Same treatment for
        # caller-supplied ``--model`` and ``--resume`` values.
        argv: list[str] = [cap.bin_path, f"--prompt={prompt}"]
        if cap.supports_streaming:
            argv += ["-o", "stream-json"]
        if request.sandbox and cap.supports_sandbox:
            argv.append("--sandbox")
        if request.model:
            argv += [f"--model={request.model}"]
        if request.session_id and cap.supports_conversation:
            argv += [f"--resume={request.session_id}"]
        return argv

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
        cap = self.detect()
        argv = self.build_command(request, log_path=log_path)

        # Refuse to spawn into a non-existent / non-directory / dangling
        # symlink cwd (defense-in-depth alongside the bridge's policy).
        try:
            cwd_resolved = resolve_cwd(request.cwd)
        except RuntimeError as exc:
            err = CanonicalEvent(
                type="error",
                subtype="invalid_cwd",
                text=self.safety.redact(str(exc)),
            )
            ctx_dummy = _RunContext(
                stdout_buf=[], stderr_buf=[], events=[],
                seen_session_id=[request.session_id],
                stop_event=threading.Event(), sink=event_sink,
                transcript_seen=set(),
            )
            self._emit(ctx_dummy, err)
            return AdapterRunResult(
                events=[err], session_id=request.session_id,
                exit_code=None, duration_ms=0, stdout_tail="",
                stderr_tail=self.safety.redact(str(exc)),
                log_path=None, artifacts=[],
            )

        ctx = _RunContext(
            stdout_buf=[],
            stderr_buf=[],
            events=[],
            seen_session_id=[request.session_id],
            stop_event=threading.Event(),
            sink=event_sink,
            transcript_seen=set(),
        )
        self._emit(ctx, _gemini_init_event(request=request, cap=cap))

        env = self._build_subprocess_env(request)
        augment_path_env_for_windows(env)
        popen_arg, _wrapped = prepare_subprocess_command(argv, env)
        start = time.time()
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(  # noqa: S603
                popen_arg,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd_resolved),
                env=env,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                # Mirror agy.py: run the child in its own group so cancel
                # can SIGTERM the subtree.
                **_process_group_kwargs(),
            )
        except OSError as exc:
            self._emit(
                ctx,
                CanonicalEvent(
                    type="error",
                    subtype="spawn_failure",
                    text=self.safety.redact(f"failed to spawn {argv[0]!r}: {exc}"),
                ),
            )
            duration = int((time.time() - start) * 1000)
            return AdapterRunResult(
                events=ctx.events,
                session_id=ctx.seen_session_id[0],
                exit_code=None,
                duration_ms=duration,
                stdout_tail="",
                stderr_tail=self.safety.redact(str(exc)),
                log_path=None,
                artifacts=[],
            )

        turn_completed = threading.Event()
        stdout_thread = threading.Thread(
            target=_stream_json_reader,
            args=(proc.stdout, ctx, self, turn_completed),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream,
            args=(proc.stderr, ctx.stderr_buf, ctx, stderr_path, "stderr", self),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        deadline = start + max(request.timeout, 1)
        exit_code: int | None = None
        timed_out = False
        cancelled = False
        completed_after_turn = False
        turn_completed_at: float | None = None
        try:
            while True:
                if proc.poll() is not None:
                    exit_code = proc.returncode
                    break
                if turn_completed.is_set():
                    if turn_completed_at is None:
                        turn_completed_at = time.time()
                    elif time.time() - turn_completed_at >= _TURN_COMPLETED_GRACE_S:
                        completed_after_turn = True
                        _shutdown_cascade(proc, escalation_cancel_event=None)
                        exit_code = 0
                        break
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    self._emit(
                        ctx,
                        CanonicalEvent(
                            type="error",
                            subtype="cancelled",
                            text="job cancelled by supervisor",
                        ),
                    )
                    # See agy.py: escalation_cancel_event=None — first
                    # cancel just initiated the cascade.
                    exit_code = _shutdown_cascade(proc, escalation_cancel_event=None)
                    break
                if time.time() >= deadline:
                    timed_out = True
                    self._emit(
                        ctx,
                        CanonicalEvent(
                            type="error",
                            subtype="wrapper_timeout",
                            text=f"gemini did not finish within {request.timeout}s",
                        ),
                    )
                    exit_code = _shutdown_cascade(proc, escalation_cancel_event=None)
                    break
                time.sleep(0.05)
        finally:
            ctx.stop_event.set()
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            # Mirror agy.py: kill the whole process group on any abnormal
            # exit so we don't orphan a subagent the child might have spawned.
            if proc is not None and proc.poll() is None:
                try:
                    _shutdown_cascade(
                        proc, escalation_cancel_event=None,
                        terminate_grace=5, kill_grace=2,
                    )
                except OSError:
                    pass
            if proc is not None:
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass

        stderr_text = "".join(ctx.stderr_buf)
        duration_ms = int((time.time() - start) * 1000)

        if (exit_code == 0 or completed_after_turn) and not timed_out and not cancelled:
            self._emit(
                ctx,
                CanonicalEvent(
                    type="result",
                    subtype="success",
                    session_id=ctx.seen_session_id[0],
                    metadata={
                        "duration_ms": duration_ms,
                        "exit_code": exit_code,
                        "terminated_after_turn_completed": completed_after_turn,
                    },
                ),
            )
        else:
            if cancelled:
                subtype = "cancelled"
            elif timed_out:
                subtype = "wrapper_timeout"
            else:
                subtype = "error"
            self._emit(
                ctx,
                CanonicalEvent(
                    type="result",
                    subtype=subtype,
                    session_id=ctx.seen_session_id[0],
                    text=self.safety.redact(stderr_text)[:2000],
                    metadata={
                        "duration_ms": duration_ms,
                        "exit_code": exit_code,
                        "timed_out": timed_out,
                        "cancelled": cancelled,
                    },
                ),
            )

        return AdapterRunResult(
            events=ctx.events,
            session_id=ctx.seen_session_id[0],
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_tail="",
            stderr_tail=truncate_middle(
                self.safety.redact(stderr_text), max_chars=request.max_output_chars
            ),
            log_path=None,
            artifacts=[],
        )

    def _emit(self, ctx: _RunContext, event: CanonicalEvent) -> None:
        """Append the event and forward to the sink with secrets stripped.

        Delegates to :meth:`BaseAdapter.emit_event` so the gemini path
        applies the same redaction policy as agy before the supervisor's
        session store sees the event.
        """

        self.emit_event(ctx, event)

    def _build_subprocess_env(self, request: BridgeRequest) -> dict[str, str]:
        """Strip host secrets before forwarding env to the gemini child.

        Same threat model as agy's ``_build_subprocess_env``: gemini-cli
        uses its own OAuth credential file under ``~/.gemini``, so wrapper-
        side provider keys (OPENAI_API_KEY, AWS_*, etc.) serve no purpose
        for it and should not be exposed in case of prompt injection.
        """

        env = self.safety.scrub_environment(dict(os.environ))
        if request.extra_env:
            env.update(self.safety.scrub_environment(dict(request.extra_env)))
        return env


# ---------------------------------------------------------------------------
# Stream-JSON parser (matches gemini-cli v0.42+ schema)
# ---------------------------------------------------------------------------

# Known event-type aliases across CLI versions: `type` or `kind` or `event`,
# `role` or `author`, `text`/`content`/`message`, `session_id`/`sessionId`/`id`.
_FIELD_TYPE = ("type", "kind", "event")
_FIELD_ROLE = ("role", "author")
_FIELD_TEXT = ("text", "content", "message")
_FIELD_SESSION = ("session_id", "sessionId", "id", "thread_id")


def _stream_json_reader(
    stream,
    ctx: _RunContext,
    adapter: GeminiCliBackend,
    turn_completed: threading.Event | None = None,
) -> None:
    if stream is None:
        return
    pending: list[str] = []
    pending_len = 0
    discarding_oversized = False
    while not ctx.stop_event.is_set():
        chunk = stream.readline(_MAX_LINE_BYTES)
        if not chunk:
            if pending and not discarding_oversized:
                _parse_stream_json_record(
                    "".join(pending).strip(),
                    ctx,
                    adapter,
                    turn_completed,
                )
            break
        if discarding_oversized:
            if chunk.endswith("\n"):
                discarding_oversized = False
            continue
        pending.append(chunk)
        pending_len += len(chunk)
        if pending_len > _MAX_STREAM_JSON_RECORD_BYTES:
            adapter._emit(
                ctx,
                CanonicalEvent(
                    type="error",
                    subtype="stream_record_too_large",
                    text=(
                        "gemini stream-json record exceeded "
                        f"{_MAX_STREAM_JSON_RECORD_BYTES} bytes"
                    ),
                ),
            )
            pending.clear()
            pending_len = 0
            discarding_oversized = not chunk.endswith("\n")
            continue
        if not chunk.endswith("\n"):
            continue
        stripped = "".join(pending).strip()
        pending.clear()
        pending_len = 0
        _parse_stream_json_record(stripped, ctx, adapter, turn_completed)


def _parse_stream_json_record(
    stripped: str,
    ctx: _RunContext,
    adapter: GeminiCliBackend,
    turn_completed: threading.Event | None,
) -> None:
    if not stripped:
        return
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="error",
                subtype="stream_decode_failure",
                text=stripped[:500],
            ),
        )
        return
    if not isinstance(payload, dict):
        return
    event = _translate_gemini_event(payload, ctx)
    if event is not None:
        adapter._emit(ctx, event)
        if event.type == "result" and event.subtype == "turn_completed":
            if turn_completed is not None:
                turn_completed.set()


def _translate_gemini_event(payload: dict, ctx: _RunContext) -> CanonicalEvent | None:
    evt_type = _first_field(payload, _FIELD_TYPE)
    role = _first_field(payload, _FIELD_ROLE)
    text = _first_field(payload, _FIELD_TEXT)
    sid = _first_field(payload, _FIELD_SESSION)
    if sid:
        with ctx.lock:
            ctx.seen_session_id[0] = str(sid)

    if evt_type == "message" and role == "assistant":
        text_value = text if isinstance(text, str) else json.dumps(text)
        return CanonicalEvent(
            type="assistant",
            subtype="text",
            session_id=ctx.seen_session_id[0],
            role="assistant",
            text=text_value,
            content=[{"type": "text", "text": text_value}],
            raw=payload,
        )
    if evt_type in ("turn.completed", "turn_completed", "completed"):
        return CanonicalEvent(
            type="result",
            subtype="turn_completed",
            session_id=ctx.seen_session_id[0],
            raw=payload,
        )
    if evt_type in ("error", "fail"):
        return CanonicalEvent(
            type="error",
            subtype=str(evt_type),
            text=str(text or payload),
            raw=payload,
        )
    # Anything else is preserved as a generic stream event so debug-mode
    # callers can inspect upstream changes.
    return CanonicalEvent(
        type="subagent_event",
        subtype=str(evt_type or "unknown"),
        raw=payload,
    )


def _first_field(payload: dict, candidates: tuple[str, ...]):
    for key in candidates:
        if key in payload:
            return payload[key]
    return None


def _gemini_init_event(*, request: BridgeRequest, cap: Capability) -> CanonicalEvent:
    return CanonicalEvent(
        type="system",
        subtype="init",
        session_id=request.session_id,
        metadata={
            "backend": "gemini",
            "bin_path": cap.bin_path,
            "version": cap.version,
            "model": request.model or cap.model,
            "cwd": request.cwd,
            "mode": request.mode,
            "sandbox": request.sandbox,
            "capabilities": {
                "streaming": cap.supports_streaming,
                "tool_use": cap.supports_tool_events,
                "resume": cap.supports_conversation,
            },
            "warnings": list(cap.warnings),
            "ts": utc_now_iso(),
        },
    )


def _parse_version(output: str) -> str | None:
    if not output:
        return None
    m = re.search(r"(\d+\.\d+\.\d+(?:[+\-]\S+)?)", output)
    return m.group(1) if m else None


__all__ = [
    "GEMINI_BINARY_NAME",
    "GeminiCliBackend",
]
