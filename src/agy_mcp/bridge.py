"""Bridge CLI — the entry point skills shell out to.

Responsibilities:

1. Parse argv into a :class:`BridgeRequest`.
2. Load :class:`Config` (config.toml + env-var overrides) and apply
   per-call defaults (worktree, allow_write, backend, output_protocol).
3. Apply :meth:`SafetyPolicy.gate_request` — deny on destructive prompts,
   reject write-mode without explicit ``--allow-write``, etc.
4. Route to ``AgyPrintBackend`` or ``GeminiCliBackend`` (auto chooses
   first available; explicit backend errors fast if unavailable).
5. Optionally create a git worktree (execute + allow_write +
   worktree_default OR --worktree explicit). Cleanup on exit.
6. Run the adapter, translate events via :class:`ProtocolTranslator`,
   and emit a :class:`BridgeResponse` JSON envelope on stdout.

The CLI never crashes the user-facing layer: errors land in a
``BridgeResponse(success=False, error=...)`` so skills get a stable JSON
shape regardless of failure mode.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any

from agy_mcp.adapters import (
    AgyPrintBackend,
    BaseAdapter,
    GeminiCliBackend,
    ListEventSink,
    ProtocolTranslator,
)
from agy_mcp.config import Config, get_config
from agy_mcp.models import (
    AdapterMetadata,
    BackendName,
    BridgeRequest,
    BridgeResponse,
    CanonicalEvent,
)
from agy_mcp.safety import SafetyPolicy, is_git_workspace
from agy_mcp.worktree import (
    WorktreeError,
    WorktreeHandle,
    cleanup_worktree,
    create_worktree,
)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agy-bridge",
        description="Skill-invoked bridge to Antigravity / Gemini CLI.",
        allow_abbrev=False,
    )
    p.add_argument("--PROMPT", required=True, help="User prompt (free text).")
    p.add_argument("--cd", default=".", help="Working directory for the child process.")
    p.add_argument(
        "--mode",
        default="ask",
        choices=["ask", "plan", "prototype", "review", "execute", "browser", "long"],
    )
    p.add_argument("--SESSION_ID", default=None,
                   help="Conversation id to resume. Empty means start fresh.")
    p.add_argument("--model", default=None,
                   help="Optional model override (forwarded to the CLI).")
    p.add_argument("--sandbox", action="store_true",
                   help="Pass --sandbox to the underlying CLI when supported.")
    p.add_argument("--allow-write", action="store_true",
                   help="Permit execute-mode writes. Required for mode=execute.")
    p.add_argument(
        "--worktree",
        choices=["true", "false", "default"],
        default="default",
        help="Override the config worktree default.",
    )
    p.add_argument("--timeout", type=int, default=900,
                   help="Wrapper-level timeout in seconds (default 900).")
    p.add_argument("--max-output-chars", type=int, default=60_000)
    p.add_argument("--backend", choices=["auto", "agy", "gemini"], default=None,
                   help="Override the config backend.")
    p.add_argument("--output-protocol", choices=["raw", "claude", "codex"], default=None,
                   help="Override the config output protocol.")
    p.add_argument("--return-all-messages", action="store_true",
                   help="Embed every translated event in the response body.")
    p.add_argument(
        "--detach", action="store_true",
        help="(reserved for Phase 4) — rejected today; pass --PROMPT directly.",
    )
    p.add_argument("--debug", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="Build argv + capability snapshot without spawning.")
    p.add_argument(
        "--extra-env",
        action="append",
        default=[],
        help="Inject ``KEY=value`` into child env (repeatable). Values are scrubbed.",
    )
    return p


_EXTRA_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# Control chars that must never appear in env values — a smuggled \n splits
# the value into a fake second variable when echoed via printenv / eval
# (Phase 3 review M5). NUL is rejected by the kernel anyway; we add CR/LF.
_EXTRA_ENV_VALUE_BANNED = re.compile(r"[\x00\r\n]")


def _parse_extra_env(items: list[str]) -> dict[str, str]:
    """Parse ``--extra-env KEY=value`` flags.

    Names that look like environment variables (uppercase + underscore +
    digit) are accepted; anything else is silently dropped — we don't want
    a poisoned flag like ``--extra-env "/etc/passwd=x"`` to reach the env.
    Values containing NUL / CR / LF are also dropped to block smuggling of
    a fake second variable into the child env.
    """

    out: dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            continue
        k, _, v = raw.partition("=")
        k = k.strip()
        if not k or not _EXTRA_ENV_NAME_RE.match(k):
            continue
        if _EXTRA_ENV_VALUE_BANNED.search(v):
            continue
        out[k] = v
    return out


def _request_from_args(args: argparse.Namespace, config: Config) -> BridgeRequest:
    worktree_arg: bool | None
    if args.worktree == "default":
        worktree_arg = None
    else:
        worktree_arg = args.worktree == "true"

    backend = args.backend or config.backend.prefer
    output_protocol = args.output_protocol or config.backend.output_protocol

    return BridgeRequest(
        prompt=args.PROMPT,
        cwd=args.cd,
        session_id=args.SESSION_ID,
        model=args.model,
        sandbox=bool(args.sandbox),
        mode=args.mode,
        return_all_messages=bool(args.return_all_messages),
        timeout=args.timeout,
        detach=bool(args.detach),
        allow_write=bool(args.allow_write),
        worktree=worktree_arg,
        max_output_chars=args.max_output_chars,
        debug=bool(args.debug),
        dry_run=bool(args.dry_run),
        backend=backend,  # type: ignore[arg-type]
        output_protocol=output_protocol,  # type: ignore[arg-type]
        extra_env=_parse_extra_env(args.extra_env),
    )


# ---------------------------------------------------------------------------
# Backend routing
# ---------------------------------------------------------------------------


def _build_adapter(
    backend: BackendName, config: Config, safety: SafetyPolicy
) -> BaseAdapter:
    if backend == "agy":
        return AgyPrintBackend(bin_override=config.backend.agy_bin, safety=safety)
    if backend == "gemini":
        return GeminiCliBackend(bin_override=config.backend.gemini_bin, safety=safety)
    raise ValueError(f"unknown backend {backend!r}")


def _select_backend(
    request: BridgeRequest, config: Config, safety: SafetyPolicy
) -> tuple[BaseAdapter, list[str]]:
    """Return (adapter, warnings). Auto routing prefers agy; falls back to gemini."""

    warnings: list[str] = []
    if request.backend in ("agy", "gemini"):
        adapter = _build_adapter(request.backend, config, safety)
        cap = adapter.detect()
        if not cap.bin_path:
            warnings.append(
                f"requested backend={request.backend!r} not available: "
                + "; ".join(cap.warnings)
            )
        return adapter, warnings

    # auto routing — lazy-probe gemini only when agy is unhealthy. Each
    # _build_adapter call re-probes, so unconditional gemini detection in the
    # healthy-agy path is wasted latency (see Phase 3 review P1.2).
    agy = _build_adapter("agy", config, safety)
    cap_agy = agy.detect()
    if cap_agy.bin_path and cap_agy.authenticated and cap_agy.supports_print:
        return agy, warnings
    gemini = _build_adapter("gemini", config, safety)
    cap_gem = gemini.detect()
    if cap_gem.bin_path and cap_gem.supports_streaming:
        warnings.append(
            "auto routing fell back to gemini-cli (agy unavailable or not authenticated)"
        )
        return gemini, warnings
    # Neither available — return agy so the caller sees the upstream warnings.
    warnings.append(
        "no backend available: agy "
        + ("ok" if cap_agy.bin_path else "missing")
        + ", gemini "
        + ("ok" if cap_gem.bin_path else "missing")
    )
    return agy, warnings


# ---------------------------------------------------------------------------
# Worktree handling
# ---------------------------------------------------------------------------


def _wants_worktree(request: BridgeRequest, config: Config) -> bool:
    if request.mode != "execute" or not request.allow_write:
        return False
    if request.worktree is not None:
        return request.worktree
    return config.execute.worktree_default


_SESSION_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]")
# Collapse runs of dots so a seed like ``foo/../bar`` doesn't survive as
# ``foo-..-bar``, which would later confuse git ref-name validation
# (Phase 3 review M4). Git rejects ``..`` in refnames; we'd rather not lean
# on git's error message for an easy upstream filter.
_SESSION_SLUG_DOT_RUN = re.compile(r"\.{2,}")


def _make_session_slug(seed: str | None) -> str:
    """Derive a worktree-safe slug from the caller-supplied session id.

    The worktree module re-validates the shape, so an attacker-supplied id
    that contains ``..`` or ``/`` is rejected fail-closed even before this
    sanitiser runs. We still scrub here so a leading hyphen (which the
    worktree regex forbids) doesn't produce a confusing error message.
    """

    if seed:
        sanitised = _SESSION_SLUG_RE.sub("-", seed)
        sanitised = _SESSION_SLUG_DOT_RUN.sub(".", sanitised)
        sanitised = sanitised.lstrip(".-_") or "session"
        return sanitised[:80]
    return "job-" + uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config = get_config()
    safety = SafetyPolicy.from_config(config)

    if args.detach:
        # Refuse loudly until Phase 4 wires the supervisor in — silent no-op
        # was the worst of both worlds (Phase 3 review P3.1).
        response = BridgeResponse(
            success=False,
            error="--detach is not implemented yet (reserved for Phase 4 supervisor)",
            cwd=args.cd,
            adapter=AdapterMetadata(),
        ).touch()
    else:
        request = _request_from_args(args, config)
        response = _run(request, config, safety)
    json.dump(response.model_dump(exclude_none=False), sys.stdout)
    sys.stdout.write("\n")
    return 0 if response.success else 1


def _run(request: BridgeRequest, config: Config, safety: SafetyPolicy) -> BridgeResponse:
    """Top-level executor; never raises — every failure becomes a BridgeResponse."""

    try:
        return _run_unsafe(request, config, safety)
    except Exception as exc:  # noqa: BLE001 - top-level guard
        # Redact + cap the traceback so a stack frame from inside the adapter
        # never leaks an absolute path or token into the response envelope.
        tb = safety.redact("".join(traceback.format_exception(exc)))[:4000]
        return BridgeResponse(
            success=False,
            error=safety.redact(str(exc)) + (" | tb=" + tb if request.debug else ""),
            cwd=request.cwd,
            adapter=AdapterMetadata(),
            command_preview=None,
            log_path=None,
        ).touch()


def _run_unsafe(
    request: BridgeRequest, config: Config, safety: SafetyPolicy
) -> BridgeResponse:
    cwd_path = Path(request.cwd).expanduser().resolve()
    gate = safety.gate_request(
        request,
        worktree_default=config.execute.worktree_default,
        is_git_workspace=is_git_workspace(cwd_path),
        cwd=cwd_path,
    )
    if not gate.allowed:
        return BridgeResponse(
            success=False,
            error=gate.reason or "request rejected by safety policy",
            cwd=str(cwd_path),
            adapter=AdapterMetadata(),
        ).touch()

    adapter, route_warnings = _select_backend(request, config, safety)
    cap = adapter.detect()
    backend_name = cap.backend

    # Short-circuit before any side effect when the routed backend has no
    # usable binary. Creating a worktree just to tear it down moments later
    # leaks state and wastes time (Phase 3 review P1.3).
    if not cap.bin_path and not request.dry_run:
        return BridgeResponse(
            success=False,
            error=" | ".join(route_warnings) or f"backend={backend_name!r} unavailable",
            warnings=list(cap.warnings),
            cwd=str(cwd_path),
            adapter=_adapter_meta(adapter, request),
        ).touch()

    worktree_handle: WorktreeHandle | None = None
    effective_cwd = cwd_path
    worktree_warnings: list[str] = []
    if _wants_worktree(request, config):
        try:
            slug = _make_session_slug(request.session_id)
            worktree_handle = create_worktree(cwd_path, slug)
            effective_cwd = worktree_handle.path
        except WorktreeError as exc:
            # Fail-closed: any execute+allow_write run that *asked for* worktree
            # isolation (either explicitly or via config default) must NOT
            # silently fall back to writing the real checkout. This was a
            # fail-open hole flagged in Phase 3 review (H2).
            return BridgeResponse(
                success=False,
                error=f"worktree creation failed: {exc}",
                warnings=route_warnings,
                cwd=str(cwd_path),
                adapter=_adapter_meta(adapter, request),
            ).touch()

    if request.dry_run:
        return _dry_run_response(
            request, adapter, effective_cwd, safety,
            route_warnings + worktree_warnings,
        )

    sink = ListEventSink()
    log_path = None
    result = None
    run_error: str | None = None
    with tempfile.TemporaryDirectory(prefix="agy-mcp-") as spool_dir:
        spool_root = Path(spool_dir)
        if cap.supports_log_file:
            log_path = spool_root / "agy.log"
        stdout_path = spool_root / "stdout.spool"
        stderr_path = spool_root / "stderr.spool"
        try:
            result = adapter.run(
                _with_cwd(request, effective_cwd),
                log_path=log_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                event_sink=sink,
            )
        except Exception as exc:  # noqa: BLE001 - keep worktree cleanup reachable
            # An exception escaping adapter.run() would otherwise skip the
            # BridgeResponse path entirely (Phase 3 review P1.1). We translate
            # it into a structured failure that still carries the warnings
            # already gathered, so the caller doesn't lose context.
            run_error = safety.redact(str(exc))
        finally:
            if worktree_handle is not None:
                try:
                    cleanup_worktree(worktree_handle, force=True)
                except WorktreeError as exc:
                    worktree_warnings.append(f"worktree cleanup failed: {exc}")

    all_warnings = [*route_warnings, *worktree_warnings, *cap.warnings]

    if result is None:
        return BridgeResponse(
            success=False,
            error=run_error or "adapter raised an unknown error",
            warnings=all_warnings,
            cwd=str(effective_cwd),
            adapter=_adapter_meta(adapter, request),
        ).touch()

    translator = ProtocolTranslator(
        request.output_protocol,
        safety=safety,
        include_raw=request.debug,
    )
    translated = translator.translate_many(result.events)

    assistant_text = _pick_assistant_text(result.events)
    success = result.exit_code == 0
    status = "completed" if success else "failed"
    all_messages = translated if request.return_all_messages else []

    return BridgeResponse(
        success=success,
        SESSION_ID=result.session_id or request.session_id or "",
        status=status,
        agent_messages=assistant_text,
        all_messages=all_messages,
        artifacts=result.artifacts,
        error=None if success else (_pick_error_text(result.events) or "non-zero exit"),
        warnings=all_warnings,
        cwd=str(effective_cwd),
        adapter=_adapter_meta(adapter, request),
        command_preview=None,
        log_path=None,  # ephemeral spool dir is gone by now
    ).touch()


def _dry_run_response(
    request: BridgeRequest,
    adapter: BaseAdapter,
    cwd: Path,
    safety: SafetyPolicy,
    warnings: list[str],
) -> BridgeResponse:
    try:
        argv = adapter.build_command(_with_cwd(request, cwd), log_path=None)
    except RuntimeError as exc:
        return BridgeResponse(
            success=False,
            error=safety.redact(str(exc)),
            warnings=warnings,
            cwd=str(cwd),
            adapter=_adapter_meta(adapter, request),
        ).touch()
    preview = safety.redact_command(argv) if request.debug else None
    return BridgeResponse(
        success=True,
        SESSION_ID=request.session_id or "",
        status="completed",
        cwd=str(cwd),
        adapter=_adapter_meta(adapter, request),
        command_preview=preview,
        warnings=warnings,
    ).touch()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _with_cwd(request: BridgeRequest, new_cwd: Path) -> BridgeRequest:
    return request.model_copy(update={"cwd": str(new_cwd)})


def _adapter_meta(adapter: BaseAdapter, request: BridgeRequest) -> AdapterMetadata:
    cap = adapter.detect()
    return AdapterMetadata(
        backend=cap.backend,
        bin_path=cap.bin_path or None,
        version=cap.version,
        model=request.model or cap.model,
        output_protocol=request.output_protocol,
        supports_streaming=cap.supports_streaming,
        supports_tool_events=cap.supports_tool_events,
    )


def _pick_assistant_text(events: list[CanonicalEvent]) -> str:
    for event in reversed(events):
        if event.type == "assistant" and event.text:
            return event.text
    return ""


def _pick_error_text(events: list[CanonicalEvent]) -> str | None:
    for event in reversed(events):
        if event.type == "error" and event.text:
            return event.text
        if event.type == "result" and event.subtype not in ("success",) and event.text:
            return event.text
    return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))


__all__ = ["main"]
