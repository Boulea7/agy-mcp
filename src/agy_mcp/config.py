"""User-facing configuration: ~/.config/agy-mcp/config.toml + env-var overrides.

Precedence (highest first):
    1. Per-call tool / CLI arguments (handled by bridge.py / server.py).
    2. Environment variables (this module).
    3. ~/.config/agy-mcp/config.toml (this module).
    4. Built-in defaults (this module).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agy_mcp.utils import expand_user_path

# ---------------------------------------------------------------------------
# Defaults — single source of truth
# ---------------------------------------------------------------------------

DEFAULT_WORKTREE = True          # user decision: execute+allow_write → worktree by default
DEFAULT_ALLOW_WRITE = False
DEFAULT_BACKEND = "auto"          # auto | agy | gemini
DEFAULT_OUTPUT_PROTOCOL = "claude"  # raw | claude | codex
DEFAULT_RETENTION_DAYS = 30


# ---------------------------------------------------------------------------
# Config file locations
# ---------------------------------------------------------------------------


def default_config_path() -> Path:
    override = os.environ.get("AGY_MCP_CONFIG")
    if override:
        return expand_user_path(override)
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return (root / "agy-mcp" / "config.toml").expanduser()


def default_session_store_root() -> Path:
    override = os.environ.get("AGY_MCP_SESSION_ROOT")
    if override:
        return expand_user_path(override)
    return (Path.home() / ".agy-mcp" / "sessions").expanduser()


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExecuteConfig:
    worktree_default: bool = DEFAULT_WORKTREE
    allow_write_default: bool = DEFAULT_ALLOW_WRITE


@dataclass(slots=True)
class BackendConfig:
    prefer: str = DEFAULT_BACKEND  # auto | agy | gemini
    output_protocol: str = DEFAULT_OUTPUT_PROTOCOL  # raw | claude | codex
    agy_bin: str | None = None
    gemini_bin: str | None = None


@dataclass(slots=True)
class SafetyConfig:
    denylist_extra: list[str] = field(default_factory=list)
    scrub_extra_env: list[str] = field(default_factory=list)
    redact_extra_patterns: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SessionStoreConfig:
    root: str = ""  # filled in from default_session_store_root()
    retention_days: int = DEFAULT_RETENTION_DAYS


@dataclass(slots=True)
class Config:
    execute: ExecuteConfig = field(default_factory=ExecuteConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    session_store: SessionStoreConfig = field(default_factory=SessionStoreConfig)
    source: str = "defaults"

    def session_store_root(self) -> Path:
        return expand_user_path(self.session_store.root) if self.session_store.root \
            else default_session_store_root()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _parse_bool_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _coerce_str(value: Any, default: str) -> str:
    if value is None:
        return default
    return str(value)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def load_config(path: Path | None = None) -> Config:
    """Load the user's config.toml (if any), then layer env-var overrides."""

    config = Config()

    target = path or default_config_path()
    if target.is_file():
        try:
            data = tomllib.loads(target.read_text(encoding="utf-8"))
            config = _from_toml(data)
            config.source = str(target)
        except (OSError, tomllib.TOMLDecodeError):
            # Config file is malformed; surface as a warning via Config.source
            # without crashing — defaults are safe.
            config.source = f"defaults (failed to read {target})"

    # Default the session store root if still empty.
    if not config.session_store.root:
        config.session_store.root = str(default_session_store_root())

    # Env-var overrides (highest layer below per-call args).
    _apply_env_overrides(config)
    return config


def _from_toml(data: dict[str, Any]) -> Config:
    execute_section = data.get("execute", {}) if isinstance(data, dict) else {}
    backend_section = data.get("backend", {}) if isinstance(data, dict) else {}
    safety_section = data.get("safety", {}) if isinstance(data, dict) else {}
    store_section = data.get("session_store", {}) if isinstance(data, dict) else {}

    execute = ExecuteConfig(
        worktree_default=bool(execute_section.get("worktree_default", DEFAULT_WORKTREE)),
        allow_write_default=bool(execute_section.get("allow_write_default", DEFAULT_ALLOW_WRITE)),
    )
    backend = BackendConfig(
        prefer=_coerce_str(backend_section.get("prefer"), DEFAULT_BACKEND),
        output_protocol=_coerce_str(backend_section.get("output_protocol"), DEFAULT_OUTPUT_PROTOCOL),
        agy_bin=backend_section.get("agy_bin") or None,
        gemini_bin=backend_section.get("gemini_bin") or None,
    )
    safety = SafetyConfig(
        denylist_extra=_coerce_str_list(safety_section.get("denylist_extra")),
        scrub_extra_env=_coerce_str_list(safety_section.get("scrub_extra_env")),
        redact_extra_patterns=_coerce_str_list(safety_section.get("redact_extra_patterns")),
    )
    session_store = SessionStoreConfig(
        root=_coerce_str(store_section.get("root"), ""),
        retention_days=_coerce_int(store_section.get("retention_days"), DEFAULT_RETENTION_DAYS),
    )
    return Config(execute=execute, backend=backend, safety=safety, session_store=session_store)


def _apply_env_overrides(config: Config) -> None:
    config.execute.worktree_default = _parse_bool_env(
        os.environ.get("AGY_MCP_WORKTREE_DEFAULT"), config.execute.worktree_default
    )
    config.execute.allow_write_default = _parse_bool_env(
        os.environ.get("AGY_MCP_ALLOW_WRITE_DEFAULT"), config.execute.allow_write_default
    )
    env_backend = os.environ.get("AGY_MCP_BACKEND")
    if env_backend:
        if env_backend in {"auto", "agy", "gemini"}:
            config.backend.prefer = env_backend
        else:
            config.source = f"{config.source} (ignored bad AGY_MCP_BACKEND={env_backend!r})"
    env_protocol = os.environ.get("AGY_MCP_OUTPUT_PROTOCOL")
    if env_protocol:
        if env_protocol in {"raw", "claude", "codex"}:
            config.backend.output_protocol = env_protocol
        else:
            config.source = f"{config.source} (ignored bad AGY_MCP_OUTPUT_PROTOCOL={env_protocol!r})"
    env_agy = os.environ.get("AGY_BIN")
    if env_agy:
        config.backend.agy_bin = env_agy
    env_gemini = os.environ.get("GEMINI_BIN")
    if env_gemini:
        config.backend.gemini_bin = env_gemini
    env_root = os.environ.get("AGY_MCP_SESSION_ROOT")
    if env_root:
        config.session_store.root = env_root
    env_retention = os.environ.get("AGY_MCP_SESSION_RETENTION_DAYS")
    if env_retention:
        config.session_store.retention_days = _coerce_int(env_retention, config.session_store.retention_days)


# Module-level singleton cache; callers may force reload via load_config().
_CACHED: Config | None = None


def get_config(*, reload: bool = False, path: Path | None = None) -> Config:
    global _CACHED
    if _CACHED is None or reload or path is not None:
        _CACHED = load_config(path=path)
    return _CACHED


__all__ = [
    "BackendConfig",
    "Config",
    "DEFAULT_ALLOW_WRITE",
    "DEFAULT_BACKEND",
    "DEFAULT_OUTPUT_PROTOCOL",
    "DEFAULT_RETENTION_DAYS",
    "DEFAULT_WORKTREE",
    "ExecuteConfig",
    "SafetyConfig",
    "SessionStoreConfig",
    "default_config_path",
    "default_session_store_root",
    "get_config",
    "load_config",
]
