"""Provider-neutral sandbox launch hooks.

The bridge cannot know whether a user's sandbox is a local VM, a locally
attached mobile device, or an external remote testing provider. This module
gives operators a narrow integration point: configure a provider command, run
it without a shell, and parse a small JSON result back into an MCP envelope.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from agy_mcp.config import Config, SandboxProviderConfig
from agy_mcp.models import SandboxControlToolResponse, SandboxStartToolResponse
from agy_mcp.safety import SafetyPolicy
from agy_mcp.utils import resolve_executable

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SANDBOX_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TIMEOUT_MAX_SECONDS = 6 * 60 * 60
_STDOUT_MAX_CHARS = 256 * 1024
_STDERR_MAX_CHARS = 64 * 1024
_LOG_TAIL_MAX_LINES = 10_000


def start_sandbox(
    *,
    config: Config,
    safety: SafetyPolicy,
    provider: str | None,
    target: str,
    cwd: str,
    scenario: str | None = None,
    timeout: int | None = None,
    dry_run: bool = False,
) -> SandboxStartToolResponse:
    """Launch a configured sandbox provider command."""

    selected_provider, provider_config = _select_provider(config, provider)
    _validate_name("target", target, pattern=_TARGET_RE)
    if scenario is not None and len(scenario) > 256:
        raise ValueError("scenario exceeds 256 characters")

    argv = _build_argv(
        _provider_command(provider_config, selected_provider, "start"),
        provider_name=selected_provider,
        action="start",
        target=target,
        cwd=cwd,
        scenario=scenario,
        sandbox_id=None,
        tail=None,
    )

    resolved_timeout = _resolve_timeout(config, timeout)

    safe_preview = [safety.redact(item) for item in argv]
    if dry_run:
        return SandboxStartToolResponse(
            success=True,
            provider=selected_provider,
            target=target,
            status="dry_run",
            command_preview=safe_preview,
        )

    workdir = Path(cwd).expanduser().resolve()
    if not workdir.is_dir():
        raise ValueError(f"cwd is not a directory: {cwd}")

    env = safety.scrub_environment(os.environ.copy())
    stdout, stderr, returncode = _run_provider(
        argv=argv,
        workdir=workdir,
        env=env,
        timeout=resolved_timeout,
    )
    if returncode != 0:
        detail = stderr.strip() or stdout.strip() or f"exit {returncode}"
        raise RuntimeError(f"sandbox provider exited {returncode}: {safety.redact(detail)}")

    payload = _parse_provider_stdout(stdout, safety=safety)
    return SandboxStartToolResponse(
        success=True,
        provider=selected_provider,
        target=target,
        status=_pick_string(payload, "status") or "running",
        sandbox_id=(
            _pick_string(payload, "sandbox_id")
            or _pick_string(payload, "id")
            or _pick_string(payload, "device_id")
            or _pick_string(payload, "vm_id")
        ),
        endpoint=(
            _pick_string(payload, "endpoint")
            or _pick_string(payload, "url")
            or _pick_string(payload, "remote_url")
        ),
        metadata=payload,
        command_preview=safe_preview,
    )


def sandbox_status(
    *,
    config: Config,
    safety: SafetyPolicy,
    provider: str | None,
    sandbox_id: str,
    cwd: str,
    timeout: int | None = None,
    dry_run: bool = False,
) -> SandboxControlToolResponse:
    """Return provider status for a previously started sandbox."""

    return _control_sandbox(
        config=config,
        safety=safety,
        action="status",
        provider=provider,
        sandbox_id=sandbox_id,
        cwd=cwd,
        timeout=timeout,
        dry_run=dry_run,
    )


def stop_sandbox(
    *,
    config: Config,
    safety: SafetyPolicy,
    provider: str | None,
    sandbox_id: str,
    cwd: str,
    timeout: int | None = None,
    dry_run: bool = False,
) -> SandboxControlToolResponse:
    """Stop a previously started sandbox."""

    return _control_sandbox(
        config=config,
        safety=safety,
        action="stop",
        provider=provider,
        sandbox_id=sandbox_id,
        cwd=cwd,
        timeout=timeout,
        dry_run=dry_run,
        default_status="stopped",
    )


def sandbox_logs(
    *,
    config: Config,
    safety: SafetyPolicy,
    provider: str | None,
    sandbox_id: str,
    cwd: str,
    tail: int = 200,
    timeout: int | None = None,
    dry_run: bool = False,
) -> SandboxControlToolResponse:
    """Fetch provider logs for a sandbox."""

    if not isinstance(tail, int) or isinstance(tail, bool):
        raise ValueError("tail must be an integer number of lines")
    if tail <= 0:
        raise ValueError("tail must be positive")
    if tail > _LOG_TAIL_MAX_LINES:
        raise ValueError(f"tail exceeds {_LOG_TAIL_MAX_LINES} lines")
    return _control_sandbox(
        config=config,
        safety=safety,
        action="logs",
        provider=provider,
        sandbox_id=sandbox_id,
        cwd=cwd,
        timeout=timeout,
        dry_run=dry_run,
        tail=tail,
    )


def sandbox_attach(
    *,
    config: Config,
    safety: SafetyPolicy,
    provider: str | None,
    sandbox_id: str,
    cwd: str,
    timeout: int | None = None,
    dry_run: bool = False,
) -> SandboxControlToolResponse:
    """Return attach/connect metadata for a sandbox."""

    return _control_sandbox(
        config=config,
        safety=safety,
        action="attach",
        provider=provider,
        sandbox_id=sandbox_id,
        cwd=cwd,
        timeout=timeout,
        dry_run=dry_run,
    )


def _control_sandbox(
    *,
    config: Config,
    safety: SafetyPolicy,
    action: str,
    provider: str | None,
    sandbox_id: str,
    cwd: str,
    timeout: int | None,
    dry_run: bool,
    tail: int | None = None,
    default_status: str = "unknown",
) -> SandboxControlToolResponse:
    selected_provider, provider_config = _select_provider(config, provider)
    _validate_name("sandbox_id", sandbox_id, pattern=_SANDBOX_ID_RE)
    argv = _build_argv(
        _provider_command(provider_config, selected_provider, action),
        provider_name=selected_provider,
        action=action,
        target=None,
        cwd=cwd,
        scenario=None,
        sandbox_id=sandbox_id,
        tail=tail,
    )
    resolved_timeout = _resolve_timeout(config, timeout)
    safe_preview = [safety.redact(item) for item in argv]
    if dry_run:
        return SandboxControlToolResponse(
            success=True,
            action=action,
            provider=selected_provider,
            sandbox_id=sandbox_id,
            status="dry_run",
            command_preview=safe_preview,
        )

    workdir = Path(cwd).expanduser().resolve()
    if not workdir.is_dir():
        raise ValueError(f"cwd is not a directory: {cwd}")
    stdout, stderr, returncode = _run_provider(
        argv=argv,
        workdir=workdir,
        env=safety.scrub_environment(os.environ.copy()),
        timeout=resolved_timeout,
    )
    if returncode != 0:
        detail = stderr.strip() or stdout.strip() or f"exit {returncode}"
        raise RuntimeError(f"sandbox provider exited {returncode}: {safety.redact(detail)}")
    payload = _parse_provider_stdout(stdout, safety=safety)
    return SandboxControlToolResponse(
        success=True,
        action=action,
        provider=selected_provider,
        sandbox_id=sandbox_id,
        status=_pick_string(payload, "status") or default_status,
        endpoint=(
            _pick_string(payload, "endpoint")
            or _pick_string(payload, "url")
            or _pick_string(payload, "remote_url")
        ),
        logs=(
            _pick_string(payload, "logs")
            or _pick_string(payload, "log")
            or _pick_string(payload, "text")
            or _pick_string(payload, "stdout")
        ),
        metadata=payload,
        command_preview=safe_preview,
    )


def _select_provider(
    config: Config,
    provider: str | None,
) -> tuple[str, SandboxProviderConfig]:
    selected_provider = provider or config.sandbox.default_provider
    if not selected_provider:
        raise ValueError(
            "sandbox provider is required; set [sandbox].default_provider "
            "or pass provider explicitly",
        )
    _validate_name("provider", selected_provider)
    provider_config = config.sandbox.providers.get(selected_provider)
    if provider_config is None:
        raise ValueError(f"sandbox provider {selected_provider!r} is not configured")
    return selected_provider, provider_config


def _provider_command(
    provider_config: SandboxProviderConfig,
    provider_name: str,
    action: str,
) -> list[str]:
    command = getattr(provider_config, action)
    if not command:
        raise ValueError(f"sandbox provider {provider_name!r} has no {action} command")
    return command


def _resolve_timeout(config: Config, timeout: int | None) -> int:
    resolved_timeout = timeout if timeout is not None else config.sandbox.default_timeout
    if not isinstance(resolved_timeout, int) or isinstance(resolved_timeout, bool):
        raise ValueError("timeout must be an integer number of seconds")
    if resolved_timeout <= 0:
        raise ValueError("timeout must be positive seconds")
    if resolved_timeout > _TIMEOUT_MAX_SECONDS:
        raise ValueError(f"timeout exceeds {_TIMEOUT_MAX_SECONDS} seconds")
    return resolved_timeout


def _run_provider(
    *,
    argv: list[str],
    workdir: Path,
    env: dict[str, str],
    timeout: int,
) -> tuple[str, str, int]:
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_file,
        tempfile.TemporaryFile(mode="w+b") as stderr_file,
    ):
        try:
            # argv is built from local config, validated for control chars, and
            # executed with shell=False. The dynamic executable is intentional.
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            proc = subprocess.Popen(
                argv,
                cwd=str(workdir),
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
            )
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            raise TimeoutError(f"sandbox provider timed out after {timeout}s") from exc
        stdout = _read_limited(stdout_file, _STDOUT_MAX_CHARS)
        stderr = _read_limited(stderr_file, _STDERR_MAX_CHARS)
    return stdout, stderr, returncode


def _read_limited(handle, limit: int) -> str:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(max(0, size - limit))
    data = handle.read(limit)
    return data.decode("utf-8", errors="replace")


def _validate_name(name: str, value: str, *, pattern: re.Pattern[str] = _NAME_RE) -> None:
    if not pattern.fullmatch(value):
        raise ValueError(f"{name} must match {pattern.pattern}")
    if value in {".", ".."}:
        raise ValueError(f"{name} must not be '.' or '..'")


def _build_argv(
    command: list[str],
    *,
    provider_name: str,
    action: str,
    target: str | None,
    cwd: str,
    scenario: str | None,
    sandbox_id: str | None,
    tail: int | None,
) -> list[str]:
    argv = [
        _format_arg(
            arg,
            provider=provider_name,
            action=action,
            target=target,
            cwd=cwd,
            scenario=scenario,
            sandbox_id=sandbox_id,
            tail=tail,
        )
        for arg in command
    ]
    executable = resolve_executable(argv[0])
    if executable is None:
        raise ValueError(f"sandbox provider command not found: {argv[0]}")
    argv[0] = executable
    return argv


def _format_arg(
    arg: str,
    *,
    provider: str,
    action: str,
    target: str | None,
    cwd: str,
    scenario: str | None,
    sandbox_id: str | None,
    tail: int | None,
) -> str:
    if not isinstance(arg, str):
        raise ValueError("sandbox provider command entries must be strings")
    try:
        value = arg.format(
            provider=provider,
            action=action,
            target=target or "",
            cwd=str(Path(cwd).expanduser()),
            scenario=scenario or "",
            sandbox_id=sandbox_id or "",
            tail="" if tail is None else str(tail),
        )
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid sandbox provider command template") from exc
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("sandbox provider command entries must not contain control chars")
    return value


def _parse_provider_stdout(stdout: str, *, safety: SafetyPolicy) -> dict[str, Any]:
    trimmed = stdout.strip()
    if len(trimmed) > _STDOUT_MAX_CHARS:
        trimmed = trimmed[:_STDOUT_MAX_CHARS]
    if not trimmed:
        return {}
    data = _load_provider_json(trimmed)
    if data is None:
        return {"stdout": safety.redact(trimmed)}
    if not isinstance(data, dict):
        return {"value": _redact_json(data, safety=safety)}
    return _redact_json(data, safety=safety)


def _load_provider_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _redact_json(value: Any, *, safety: SafetyPolicy) -> Any:
    if isinstance(value, str):
        return safety.redact(value)
    if isinstance(value, list):
        return [_redact_json(item, safety=safety) for item in value]
    if isinstance(value, dict):
        return {
            safety.redact(str(key)): _redact_json(item, safety=safety)
            for key, item in value.items()
        }
    return value


def _pick_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


__all__ = [
    "sandbox_attach",
    "sandbox_logs",
    "sandbox_status",
    "start_sandbox",
    "stop_sandbox",
]
