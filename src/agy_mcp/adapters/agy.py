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
import re
import stat
import subprocess
import threading
import time
from pathlib import Path

from agy_mcp.adapters.base import (
    _MAX_LINE_BYTES,
    AdapterRunResult,
    BaseAdapter,
    EventSink,
    _drain_stream,
    _process_group_kwargs,
    _RunContext,
    _shutdown_cascade,
    has_flag,
    resolve_cwd,
)
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
# Real agy conversation ids are UUIDs (8-4-4-4-12 hex). Require ``>=8`` hex
# chars in the first segment and ``>=2`` per subsequent dash-separated
# group, so trailing junk like ``...-extra`` is rejected and we never
# capture a sub-UUID prefix like a stray ``abcd``.
_RE_CREATED_CONV = re.compile(r"Created conversation ([0-9a-fA-F]{8,}(?:-[0-9a-fA-F]{2,})*)")
# Tolerate format drift: ``Print mode: starting (k1=v1, k2="v2", ...)``.
# We match the prefix, then extract ``key=value`` pairs from the body.
# The body class uses ``[^)]*`` so values containing literal ``)`` would
# still close the prefix early — that mirrors how klog itself escapes such
# characters (they never appear in the wild) and keeps the regex linear.
_RE_PRINT_START_PREFIX = re.compile(r"Print mode: starting \((?P<body>[^)]*)\)")
_RE_PRINT_START_KV = re.compile(r'(?P<k>\w+)=(?:"(?P<qv>[^"]*)"|(?P<rv>[^,\s)]+))')
_RE_RESUMING_CONV = re.compile(
    r"Print mode: resuming conversation ([0-9a-fA-F]{8,}(?:-[0-9a-fA-F]{2,})*)"
)
_RE_NEW_CONV = re.compile(r"Starting new conversation \(agent=(true|false)\)")
_RE_AUTO_FLUSH = re.compile(
    r"Auto-flush: sending (\d+) queued input\(s\) \(combinedLength=(\d+), media=(\d+)\)"
)
_RE_SEND_FAILED = re.compile(r"Print mode: SendUserMessage failed: (.+)")
_RE_AUTH_TIMEOUT = re.compile(r"Print mode: auth timed out")
_RE_AUTH_ERROR = re.compile(r"Print mode: auth error: (.+)")
_RE_REWIND = re.compile(r"Rewinding conversation [0-9a-fA-F-]+ to step (\d+)")
_RE_STREAM_START = re.compile(r"Starting conversation update stream for ([0-9a-fA-F-]+)\b")
_RE_TURN_END = re.compile(r"Stopping conversation stream|Language server shutting down")


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


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
            authenticated=_is_regular_file(AGY_OAUTH_CREDS_PATH),
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
        """Read the active model label from agy's settings file (read-only).

        Refuses to follow symlinks so a malicious ``settings.json -> ~/.ssh/id_rsa``
        cannot trick us into echoing private content into a parse-failure log.
        """

        for path in (AGY_SETTINGS_PATH, AGY_GEMINI_SETTINGS_PATH):
            try:
                if not path.is_file() or path.is_symlink():
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
                "agy binary not found; set AGY_BIN or backend.agy_bin. "
                f"{cap.warnings!r}"
            )
        if not cap.supports_print:
            raise RuntimeError(
                "Installed `agy` does not advertise --print; check `agy --help`."
            )
        # H1 (Phase 3 review): use ``--print=<prompt>`` rather than
        # ``--print <prompt>`` so a hostile prompt starting with ``--`` (e.g.
        # ``--dangerously-skip-permissions``) cannot peel off into a fresh
        # flag. The fused form keeps the prompt inside a single argv element
        # regardless of how the downstream CLI's parser handles values that
        # look like flags.
        argv: list[str] = [
            cap.bin_path,
            f"--print={self._prepare_prompt(request.prompt)}",
        ]

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
                # ``--conversation=<id>`` for the same reason as --print=
                # above: session_id is caller-supplied and could be crafted
                # to look like a flag.
                argv += [f"--conversation={request.session_id}"]
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
        cancel_event: threading.Event | None = None,
    ) -> AdapterRunResult:
        cap = self.detect()
        argv = self.build_command(request, log_path=log_path)

        # Refuse to spawn into a non-existent / non-directory / dangling
        # symlink cwd; this is defense-in-depth in addition to the bridge's
        # workspace-allowlist policy (Phase 3+).
        try:
            cwd_resolved = resolve_cwd(request.cwd)
        except RuntimeError as exc:
            err = CanonicalEvent(
                type="error",
                subtype="invalid_cwd",
                text=self.safety.redact(str(exc)),
            )
            self._emit(_RunContext(
                stdout_buf=[], stderr_buf=[], events=[],
                seen_session_id=[request.session_id],
                stop_event=threading.Event(), sink=event_sink,
                transcript_seen=set(),
            ), err)
            return AdapterRunResult(
                events=[err], session_id=request.session_id,
                exit_code=None, duration_ms=0, stdout_tail="",
                stderr_tail=self.safety.redact(str(exc)),
                log_path=str(log_path) if log_path else None,
                artifacts=[],
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

        self._emit(ctx, _system_init_event(request=request, cap=cap))

        env = self._build_subprocess_env(request)
        augment_path_env_for_windows(env)
        popen_arg, _wrapped = prepare_subprocess_command(argv, env)
        start = time.time()
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv built from probed cap
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
                # Put the child into its own process group / session so we
                # can SIGTERM the whole subtree (agy spawns a grpc sidecar
                # — leaving it behind would orphan a language server).
                # Windows uses CREATE_NEW_PROCESS_GROUP via creationflags;
                # on POSIX start_new_session=True does the same job. Only
                # the platform-appropriate kwarg is passed to avoid the
                # cross-platform TypeError.
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
                log_path=str(log_path) if log_path else None,
                artifacts=[],
            )

        threads: list[threading.Thread] = []
        threads.append(
            threading.Thread(
                target=_drain_stream,
                args=(proc.stdout, ctx.stdout_buf, ctx, stdout_path, "stdout", self),
                daemon=True,
            )
        )
        threads.append(
            threading.Thread(
                target=_drain_stream,
                args=(proc.stderr, ctx.stderr_buf, ctx, stderr_path, "stderr", self),
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
        cancelled = False
        try:
            while True:
                if proc.poll() is not None:
                    exit_code = proc.returncode
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
                    # escalation_cancel_event=None: the first cancel just
                    # initiated this cascade; passing the same event back
                    # in would let a redundant re-check shortcut SIGKILL.
                    # Double-cancel UX is tracked in followups.md.
                    exit_code = _shutdown_cascade(proc, escalation_cancel_event=None)
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
                    # See cancel branch comment above re: None.
                    exit_code = _shutdown_cascade(proc, escalation_cancel_event=None)
                    break
                time.sleep(_TAIL_POLL_INTERVAL_S)
        finally:
            ctx.stop_event.set()
            for t in threads:
                t.join(timeout=5)
            # Even if the outer try-block raised (KeyboardInterrupt, etc.)
            # we must not orphan the child or its sidecar processes.
            # Use the same terminate -> wait -> kill cascade on the whole
            # process group so the grpc language server gets cleaned up.
            if proc is not None and proc.poll() is None:
                try:
                    # Cleanup-only cascade — no cancel signal here.
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
        if exit_code == 0 and not timed_out and not cancelled:
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
                        "conversation_id": ctx.seen_session_id[0],
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
        """Append the event and forward to the sink with secrets stripped.

        Delegates to the shared ``BaseAdapter.emit_event`` which applies
        recursive redaction before the event reaches the sink (and the
        supervisor's session store behind it).
        """

        self.emit_event(ctx, event)

    def _build_subprocess_env(self, request: BridgeRequest) -> dict[str, str]:
        """Strip host secrets before forwarding env to the agy child.

        The child has its own OAuth file under ``~/.gemini`` so wrapper-side
        provider keys (OPENAI_API_KEY, AWS_*, GITHUB_TOKEN, etc.) serve no
        purpose for it — but a prompt-injection inside agy that runs
        ``printenv`` would otherwise exfiltrate them. We replace the values
        with the redaction placeholder rather than deleting the names so
        downstream tooling can still detect "this var existed".
        """

        env = self.safety.scrub_environment(dict(os.environ))
        # Caller-provided extras are still scrubbed by key. Wrapper-owned
        # controls are written after this block so even a constructed
        # BridgeRequest cannot re-enable auto-update or alter the session id.
        if request.extra_env:
            env.update(self.safety.scrub_environment(dict(request.extra_env)))
        if request.session_id:
            env["ANTIGRAVITY_CONVERSATION_ID"] = request.session_id
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "1"
        return env


# ---------------------------------------------------------------------------
# Process-group helpers (``_process_group_kwargs``, ``_terminate_group``,
# ``_kill_group``) and the cancel/timeout cascade (``_shutdown_cascade``)
# live in ``adapters/base`` since Phase 4 R1 P2#8 so every adapter that
# spawns a subtree reuses the same code without sibling imports.
# ---------------------------------------------------------------------------


def _tail_klog(log_path: Path, ctx: _RunContext, adapter: AgyPrintBackend) -> None:
    """Tail the --log-file written by agy and emit structured lifecycle events.

    The final-drain pass lives in the ``finally`` block so it always runs —
    short-lived subprocesses that exit before the first poll-loop tick must
    not lose their lifecycle events.
    """

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
            line = fp.readline(_MAX_LINE_BYTES)
            if not line:
                if ctx.stop_event.is_set():
                    break
                time.sleep(_TAIL_POLL_INTERVAL_S)
                continue
            _handle_klog_line(line, ctx, adapter)
    finally:
        # Always attempt a final drain — covers the race where stop_event
        # fired before fp was opened, and the normal-exit case where there
        # are buffered lines past the last successful readline.
        if fp is None and log_path.exists():
            try:
                fp = log_path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                fp = None
        if fp is not None:
            try:
                while True:
                    remainder = fp.readline(_MAX_LINE_BYTES)
                    if not remainder:
                        break
                    _handle_klog_line(remainder, ctx, adapter)
            finally:
                try:
                    fp.close()
                except OSError:
                    pass


def _handle_klog_line(line: str, ctx: _RunContext, adapter: AgyPrintBackend) -> None:
    match = _KLOG_LINE.match(line)
    msg_full = (match.group("msg") if match else line).strip()
    if not msg_full:
        return
    # Cap and redact the raw text we store under metadata.raw so an attacker
    # who can write to the klog file (or a future agy version that logs
    # secrets in error paths) cannot leak them through the event sink.
    msg = msg_full[:2000]
    safe_msg = adapter.safety.redact(msg)

    if m := _RE_GRPC_PORT.search(msg):
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="sidecar_ready",
                metadata={"grpc_port": int(m.group(1)), "raw": safe_msg},
            ),
        )
        return
    if m := _RE_HTTP_PORT.search(msg):
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="sidecar_http_ready",
                metadata={"http_port": int(m.group(1)), "raw": safe_msg},
            ),
        )
        return
    if m := _RE_CREATED_CONV.search(msg):
        sid = m.group(1)
        with ctx.lock:
            ctx.seen_session_id[0] = sid
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="conversation_started",
                session_id=sid,
                metadata={"raw": safe_msg},
            ),
        )
        return
    if m := _RE_RESUMING_CONV.search(msg):
        sid = m.group(1)
        with ctx.lock:
            ctx.seen_session_id[0] = sid
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="conversation_resumed",
                session_id=sid,
                metadata={"raw": safe_msg},
            ),
        )
        return
    if m := _RE_PRINT_START_PREFIX.search(msg):
        # Two-pass: extract the body, then parse key=value pairs from it.
        # Tolerates extra/missing fields between agy versions.
        body = m.group("body")
        kv = {
            pair.group("k"): (pair.group("qv") if pair.group("qv") is not None else pair.group("rv"))
            for pair in _RE_PRINT_START_KV.finditer(body)
        }
        try:
            prompt_len = int(kv.get("promptLength", "0"))
        except ValueError:
            prompt_len = 0
        model = kv.get("model") or None
        sid = kv.get("conversationID") or None
        if sid:
            with ctx.lock:
                ctx.seen_session_id[0] = sid
        adapter._emit(
            ctx,
            CanonicalEvent(
                type="system",
                subtype="print_starting",
                session_id=sid,
                metadata={"prompt_length": prompt_len, "model": model, "fields": kv},
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
            CanonicalEvent(type="system", subtype="turn_end", metadata={"raw": safe_msg}),
        )
        return


def _tail_transcripts(ctx: _RunContext, adapter: AgyPrintBackend, started_at: float) -> None:
    """Best-effort watcher for subagent transcript.jsonl files.

    Symlinks are skipped, and each candidate must resolve to a path that is
    actually contained under ``AGY_LOG_DIR`` — otherwise a hostile entry
    under ~/.gemini/antigravity-cli/log/ (e.g. a symlink to ~/.ssh/id_rsa)
    could be read and its path leaked into events.
    """

    if not AGY_LOG_DIR.exists():
        return
    try:
        log_root = AGY_LOG_DIR.resolve(strict=True)
    except (OSError, RuntimeError):
        return
    while not ctx.stop_event.is_set():
        try:
            for candidate in AGY_LOG_DIR.rglob("transcript.jsonl"):
                if candidate in ctx.transcript_seen:
                    continue
                # Defense: refuse symlinks outright. They're never written
                # by agy itself; if one appears, treat it as suspicious.
                try:
                    if candidate.is_symlink():
                        ctx.transcript_seen.add(candidate)
                        continue
                    resolved = candidate.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                try:
                    resolved.relative_to(log_root)
                except ValueError:
                    # Resolved outside the log root — skip without echoing
                    # the path so we don't leak the symlink target.
                    ctx.transcript_seen.add(candidate)
                    continue
                try:
                    if resolved.stat().st_mtime < started_at - 1:
                        ctx.transcript_seen.add(candidate)
                        continue
                except OSError:
                    continue
                ctx.transcript_seen.add(candidate)
                _drain_transcript(resolved, ctx, adapter)
        except OSError:
            pass
        if ctx.stop_event.wait(timeout=0.5):
            return


def _drain_transcript(path: Path, ctx: _RunContext, adapter: AgyPrintBackend) -> None:
    """Pass-through every NDJSON line of a transcript.jsonl as subagent_event."""

    try:
        fp = _open_transcript_no_follow(path)
    except OSError:
        return
    try:
        while not ctx.stop_event.is_set():
            line = fp.readline(_MAX_LINE_BYTES)
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


def _open_transcript_no_follow(path: Path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"refusing to read non-regular transcript: {path}")
        return os.fdopen(fd, "r", encoding="utf-8", errors="replace")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


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


def _is_regular_file(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


__all__ = [
    "AGY_BINARY_NAME",
    "AGY_OAUTH_CREDS_PATH",
    "AGY_SETTINGS_PATH",
    "AgyPrintBackend",
]
