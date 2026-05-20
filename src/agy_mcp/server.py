"""FastMCP server exposing the agy-mcp toolkit over stdio.

Tools (all return dicts with stable keys; never raise across the wire):

* ``agy`` — synchronous one-shot bridge call (compatible with upstream-reference).
* ``agy_start`` — spawn a background job, return ``status="running"`` envelope.
* ``agy_continue`` — like ``agy``, but ``SESSION_ID`` is required.
* ``agy_status`` — poll a running job's :class:`JobRecord`.
* ``agy_read`` — read events from a job (raw or translated).
* ``agy_cancel`` — signal a running job to stop.
* ``agy_sessions`` — list recent jobs.
* ``agy_doctor`` — environment + capability probe.
* ``agy_install_skill`` — write the scaffold skill into target dirs.

Threading model: the FastMCP runtime drives tools from an asyncio loop and
calls sync tool functions inline. ``agy`` and ``agy_continue`` would block
that loop while ``_bridge_run`` waits on a subprocess, so they are declared
``async def`` and dispatch the blocking work to a worker thread via
:func:`anyio.to_thread.run_sync` (Phase 5 R1 arch P1.1).

Every tool routes its output through :class:`SafetyPolicy` before
serialisation — adapter buffers, capability warnings, and error strings
have already been scrubbed by the lower layers, but the doctor / install
helpers also redact their own paths so a transcript capture never leaks
a ``/Users/<user>/`` path.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

from agy_mcp import __version__
from agy_mcp.adapters.agy import AgyPrintBackend
from agy_mcp.adapters.gemini import GeminiCliBackend
from agy_mcp.bridge import _run as _bridge_run
from agy_mcp.config import Config, get_config
from agy_mcp.doctor import run_doctor
from agy_mcp.install import SkillScope, SkillTarget, install_skills
from agy_mcp.models import (
    BackendName,
    BridgeRequest,
    BridgeResponse,
    Mode,
    OutputProtocol,
)
from agy_mcp.safety import SafetyPolicy
from agy_mcp.session_store import SessionStore
from agy_mcp.supervisor import Supervisor

# ---------------------------------------------------------------------------
# Module-level singletons. The FastMCP runtime imports this module exactly
# once per process; the singletons are lazily materialised on the first tool
# call so importing ``agy_mcp.server`` for tests stays cheap.
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_config: Config | None = None
_safety: SafetyPolicy | None = None
_store: SessionStore | None = None
_supervisor: Supervisor | None = None
# Cached adapters for the doctor probe so we don't pay 4 subprocess calls
# (one help + one version per backend) on every ``agy_doctor`` invocation.
# Phase 5 R1 arch P1.5.
_agy_adapter: AgyPrintBackend | None = None
_gemini_adapter: GeminiCliBackend | None = None

# Defence-in-depth cap so a malicious or buggy caller can't burn unbounded
# memory by passing a multi-megabyte string in place of a job_id slug.
_MAX_JOB_ID_LEN = 256


def _ensure_state() -> tuple[Config, SafetyPolicy, SessionStore, Supervisor]:
    global _config, _safety, _store, _supervisor
    with _state_lock:
        if _config is None:
            _config = get_config()
        if _safety is None:
            _safety = SafetyPolicy.from_config(_config)
        if _store is None:
            _store = SessionStore(Path(_config.session_store_root()).expanduser())
        if _supervisor is None:
            _supervisor = Supervisor(
                store=_store, config=_config, safety=_safety,
            )
        return _config, _safety, _store, _supervisor


def _ensure_adapters() -> tuple[AgyPrintBackend, GeminiCliBackend]:
    """Lazily build doctor adapter singletons.

    Each adapter probes its CLI exactly once (caching the result), so the
    doctor probe can reuse them across calls instead of forking
    ``agy --help`` / ``agy --version`` / ``gemini --help`` /
    ``gemini --version`` every invocation. The MCP server is the only
    caller; tests bypass this by passing fresh adapters directly to
    ``run_doctor``.
    """

    global _agy_adapter, _gemini_adapter
    _, safety, _store_, _supervisor_ = _ensure_state()
    with _state_lock:
        if _agy_adapter is None:
            _agy_adapter = AgyPrintBackend(safety=safety)
        if _gemini_adapter is None:
            _gemini_adapter = GeminiCliBackend(safety=safety)
        return _agy_adapter, _gemini_adapter


def _reset_state_for_tests() -> None:
    """Drop the cached singletons so tests can swap in fresh stores."""

    global _config, _safety, _store, _supervisor, _agy_adapter, _gemini_adapter
    with _state_lock:
        _config = None
        _safety = None
        _store = None
        _supervisor = None
        _agy_adapter = None
        _gemini_adapter = None


# ---------------------------------------------------------------------------
# FastMCP instance and tool registrations
# ---------------------------------------------------------------------------


mcp = FastMCP(
    name="agy-mcp",
    instructions=(
        "Google Antigravity (agy) CLI bridge with long-task supervisor. "
        "Use ``agy`` for one-shot prompts, ``agy_start`` + ``agy_status`` "
        "+ ``agy_read`` + ``agy_cancel`` for detached jobs, and "
        "``agy_doctor`` to check the environment."
    ),
)


# ---------------------------------------------------------------------------
# Helpers used by the synchronous tools (``agy``, ``agy_continue``)
# ---------------------------------------------------------------------------


def _build_request(payload: dict[str, Any]) -> BridgeRequest:
    """Validate the incoming MCP arguments through the BridgeRequest schema.

    Pydantic raises ``ValidationError`` on bad input; the tool wrapper
    catches it and converts to a structured failure envelope.
    """

    return BridgeRequest(**payload)


def _structured_failure(safety: SafetyPolicy, exc: BaseException, *, cwd: str = "") -> dict[str, Any]:
    """Top-level guard: any tool exception becomes a structured envelope."""

    return BridgeResponse(
        success=False,
        error=safety.redact(str(exc)),
        cwd=safety.redact(cwd),
    ).model_dump(mode="json")


def _response_to_dict(resp: BridgeResponse) -> dict[str, Any]:
    return resp.model_dump(mode="json")


def _validate_job_id(safety: SafetyPolicy, job_id: str) -> str | None:
    """Return a redacted error string if ``job_id`` is invalid, else None."""

    if not job_id:
        return safety.redact("job_id is required")
    if len(job_id) > _MAX_JOB_ID_LEN:
        return safety.redact(
            f"job_id exceeds {_MAX_JOB_ID_LEN} chars; refusing to look up",
        )
    return None


# ---------------------------------------------------------------------------
# Tool: agy — synchronous one-shot
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy",
    description=(
        "Run agy --print synchronously and return the assistant text + "
        "metadata. Compatible drop-in for the legacy `gemini` tool: same "
        "PROMPT / cd / sandbox / SESSION_ID / return_all_messages / model "
        "fields, with new mode / timeout / allow_write / worktree / backend "
        "/ output_protocol options."
    ),
)
async def agy_tool(
    PROMPT: str,
    cd: str = ".",
    SESSION_ID: str | None = None,
    model: str | None = None,
    sandbox: bool = False,
    return_all_messages: bool = False,
    mode: Mode = "ask",
    timeout: int = 900,
    allow_write: bool = False,
    worktree: bool | None = None,
    backend: BackendName = "auto",
    output_protocol: OutputProtocol = "claude",
    debug: bool = False,
    dry_run: bool = False,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    config, safety, _store_, _supervisor_ = _ensure_state()
    try:
        request = _build_request(
            {
                "prompt": PROMPT,
                "cwd": cd,
                "session_id": SESSION_ID,
                "model": model,
                "sandbox": sandbox,
                "return_all_messages": return_all_messages,
                "mode": mode,
                "timeout": timeout,
                "allow_write": allow_write,
                "worktree": worktree,
                "backend": backend,
                "output_protocol": output_protocol,
                "debug": debug,
                "dry_run": dry_run,
                "extra_env": extra_env or {},
            }
        )
    except Exception as exc:  # noqa: BLE001 - validation guard
        return _structured_failure(safety, exc, cwd=cd)
    # ``_bridge_run`` launches the agy subprocess and blocks until it
    # finishes — offload to a worker thread so the FastMCP asyncio loop
    # stays free to dispatch other tool calls. (Phase 5 R1 arch P1.1)
    response = await anyio.to_thread.run_sync(
        _bridge_run, request, config, safety,
    )
    return _response_to_dict(response)


# ---------------------------------------------------------------------------
# Tool: agy_continue — same as agy but session_id is required
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_continue",
    description=(
        "Continue an existing agy session. Identical to `agy` except "
        "SESSION_ID is required and the underlying adapter resumes the "
        "Antigravity conversation."
    ),
)
async def agy_continue_tool(
    SESSION_ID: str,
    PROMPT: str,
    cd: str = ".",
    model: str | None = None,
    sandbox: bool = False,
    return_all_messages: bool = False,
    mode: Mode = "ask",
    timeout: int = 900,
    allow_write: bool = False,
    worktree: bool | None = None,
    backend: BackendName = "auto",
    output_protocol: OutputProtocol = "claude",
    debug: bool = False,
    dry_run: bool = False,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    config, safety, _store_, _supervisor_ = _ensure_state()
    if not SESSION_ID:
        return _structured_failure(
            safety, ValueError("SESSION_ID is required for agy_continue"), cwd=cd,
        )
    try:
        request = _build_request(
            {
                "prompt": PROMPT,
                "cwd": cd,
                "session_id": SESSION_ID,
                "model": model,
                "sandbox": sandbox,
                "return_all_messages": return_all_messages,
                "mode": mode,
                "timeout": timeout,
                "allow_write": allow_write,
                "worktree": worktree,
                "backend": backend,
                "output_protocol": output_protocol,
                "debug": debug,
                "dry_run": dry_run,
                "extra_env": extra_env or {},
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _structured_failure(safety, exc, cwd=cd)
    response = await anyio.to_thread.run_sync(
        _bridge_run, request, config, safety,
    )
    return _response_to_dict(response)


# ---------------------------------------------------------------------------
# Tool: agy_start — spawn a background job
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_start",
    description=(
        "Start an agy session in the background. Returns an envelope with "
        "status='running' and a job_id you can poll via agy_status / "
        "agy_read / agy_cancel."
    ),
)
def agy_start_tool(
    PROMPT: str,
    cd: str = ".",
    SESSION_ID: str | None = None,
    model: str | None = None,
    sandbox: bool = False,
    mode: Mode = "ask",
    timeout: int = 900,
    allow_write: bool = False,
    worktree: bool | None = None,
    backend: BackendName = "auto",
    output_protocol: OutputProtocol = "claude",
    debug: bool = False,
    extra_env: dict[str, str] | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    config, safety, _store_, supervisor = _ensure_state()
    try:
        request = _build_request(
            {
                "prompt": PROMPT,
                "cwd": cd,
                "session_id": SESSION_ID,
                "model": model,
                "sandbox": sandbox,
                "return_all_messages": False,
                "mode": mode,
                "timeout": timeout,
                "detach": True,
                "allow_write": allow_write,
                "worktree": worktree,
                "backend": backend,
                "output_protocol": output_protocol,
                "debug": debug,
                "extra_env": extra_env or {},
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _structured_failure(safety, exc, cwd=cd)
    if job_id is not None:
        err = _validate_job_id(safety, job_id)
        if err is not None:
            return _structured_failure(safety, ValueError(err), cwd=cd)
    try:
        response = supervisor.start(request, job_id=job_id)
    except Exception as exc:  # noqa: BLE001 - top-level guard
        return _structured_failure(safety, exc, cwd=cd)
    return _response_to_dict(response)


# ---------------------------------------------------------------------------
# Tool: agy_status — poll a job's JobRecord
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_status",
    description="Return the JobRecord (status, exit code, error, timestamps) for a job_id.",
)
def agy_status_tool(job_id: str) -> dict[str, Any]:
    config, safety, _store_, supervisor = _ensure_state()
    err = _validate_job_id(safety, job_id)
    if err is not None:
        return _structured_failure(safety, ValueError(err))
    try:
        record = supervisor.status(job_id)
    except Exception as exc:  # noqa: BLE001
        return _structured_failure(safety, exc)
    if record is None:
        # Use the same envelope shape as other failures so consumers can
        # rely on ``success/error`` keys regardless of why the lookup
        # failed. (Phase 5 R1 arch P1.3)
        return _structured_failure(
            safety, ValueError(f"job_id {job_id!r} not found"),
        )
    return {"success": True, "record": record.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Tool: agy_read — read events from a job
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_read",
    description=(
        "Read events from a job's event log. ``since`` is the 0-based offset; "
        "``translate`` may be 'raw', 'claude', or 'codex' to wire-format the "
        "events (default returns canonical events as dicts)."
    ),
)
def agy_read_tool(
    job_id: str,
    since: int = 0,
    translate: str | None = None,
) -> dict[str, Any]:
    config, safety, _store_, supervisor = _ensure_state()
    err = _validate_job_id(safety, job_id)
    if err is not None:
        return _structured_failure(safety, ValueError(err))
    try:
        if translate is None:
            events = supervisor.read_events(job_id, since=since)
            payload: list[dict[str, Any]] = [
                e.model_dump(mode="json") for e in events
            ]
        else:
            payload = supervisor.read_translated(
                job_id, since=since, protocol=translate,
            )
    except Exception as exc:  # noqa: BLE001
        return _structured_failure(safety, exc)
    return {
        "success": True,
        "job_id": job_id,
        "since": since,
        "translate": translate,
        "events": payload,
        "count": len(payload),
    }


# ---------------------------------------------------------------------------
# Tool: agy_cancel — signal a running job
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_cancel",
    description=(
        "Signal a running job to stop. Returns ``success=True, signalled=True`` "
        "if the worker was alive, ``signalled=False`` if it was unknown / "
        "already finished."
    ),
)
def agy_cancel_tool(job_id: str) -> dict[str, Any]:
    config, safety, _store_, supervisor = _ensure_state()
    err = _validate_job_id(safety, job_id)
    if err is not None:
        return _structured_failure(safety, ValueError(err))
    try:
        signalled = supervisor.cancel(job_id)
    except Exception as exc:  # noqa: BLE001
        return _structured_failure(safety, exc)
    return {"success": True, "job_id": job_id, "signalled": signalled}


# ---------------------------------------------------------------------------
# Tool: agy_sessions — list recent jobs
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_sessions",
    description=(
        "List recent jobs, newest first. ``limit`` defaults to 50; pass 0 "
        "for the full list."
    ),
)
def agy_sessions_tool(limit: int = 50) -> dict[str, Any]:
    config, safety, _store_, supervisor = _ensure_state()
    effective: int | None = limit if limit > 0 else None
    try:
        records = supervisor.list_sessions(limit=effective)
    except Exception as exc:  # noqa: BLE001
        return _structured_failure(safety, exc)
    return {
        "success": True,
        "count": len(records),
        "records": [r.model_dump(mode="json") for r in records],
    }


# ---------------------------------------------------------------------------
# Tool: agy_doctor — environment probe
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_doctor",
    description=(
        "Run capability + auth + session-store probes. Returns a structured "
        "report (no secrets) suitable for surfacing to a user via MCP."
    ),
)
def agy_doctor_tool() -> dict[str, Any]:
    config, safety, store, _supervisor_ = _ensure_state()
    try:
        agy_adapter, gemini_adapter = _ensure_adapters()
    except Exception as exc:  # noqa: BLE001 - never let init crash the tool
        return _structured_failure(safety, exc)
    try:
        report = run_doctor(
            config=config,
            safety=safety,
            agy_adapter=agy_adapter,
            gemini_adapter=gemini_adapter,
            session_store=store,
        )
    except Exception as exc:  # noqa: BLE001
        return _structured_failure(safety, exc)
    return {"success": True, "report": report.to_dict(), "version": __version__}


# ---------------------------------------------------------------------------
# Tool: agy_install_skill — write scaffold skill into target dirs
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_install_skill",
    description=(
        "Install the agy-mcp collaboration skill into one or more agent "
        "platforms. ``targets`` may include 'claude', 'codex', "
        "'antigravity', or 'all' (default expands to claude+codex; "
        "antigravity is opt-in via an explicit target list). ``scope`` is "
        "'user' (default) or 'project'; project scope requires "
        "``project_root``."
    ),
)
def agy_install_skill_tool(
    targets: list[str] | None = None,
    scope: SkillScope = "user",
    project_root: str | None = None,
) -> dict[str, Any]:
    config, safety, _store_, _supervisor_ = _ensure_state()
    if scope not in ("user", "project"):
        return _structured_failure(
            safety, ValueError(f"scope must be 'user' or 'project', got {scope!r}"),
        )
    chosen_targets = targets if targets else ["all"]
    try:
        validated_targets: list[SkillTarget] = [t for t in chosen_targets]  # type: ignore[list-item]
        result = install_skills(
            targets=validated_targets,
            scope=scope,
            project_root=Path(project_root) if project_root else None,
            safety=safety,
        )
    except Exception as exc:  # noqa: BLE001
        return _structured_failure(safety, exc)
    return result.to_dict()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> None:
    """Start the FastMCP stdio server."""

    # Materialise singletons before stdio takes over so any configuration
    # error surfaces with a stack trace rather than a closed pipe.
    _ensure_state()
    mcp.run(transport="stdio")


__all__ = [
    "agy_cancel_tool",
    "agy_continue_tool",
    "agy_doctor_tool",
    "agy_install_skill_tool",
    "agy_read_tool",
    "agy_sessions_tool",
    "agy_start_tool",
    "agy_status_tool",
    "agy_tool",
    "mcp",
    "run",
]
