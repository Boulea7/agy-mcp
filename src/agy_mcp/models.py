"""Pydantic models that define the stable bridge / MCP JSON schema."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Enums (string literals — kept as Literal for trivial JSON round-trips)
# ---------------------------------------------------------------------------

Mode = Literal["ask", "plan", "prototype", "review", "execute", "browser", "long"]
BackendName = Literal["auto", "agy", "gemini"]
OutputProtocol = Literal["raw", "claude", "codex"]
JobStatus = Literal["completed", "running", "failed", "cancelled", "unknown"]

# ---------------------------------------------------------------------------
# Capability — runtime detection result
# ---------------------------------------------------------------------------


class Capability(BaseModel):
    """Result of probing a CLI backend (`agy` or `gemini`) at startup.

    All ``supports_*`` flags should be derived from a help/version probe and
    cached per binary path so we never hardcode a vendor-specific flag set.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    bin_path: str
    backend: Literal["agy", "gemini"]
    version: str | None = None
    supports_print: bool = False
    supports_print_timeout: bool = False
    supports_conversation: bool = False
    supports_continue: bool = False
    supports_sandbox: bool = False
    supports_log_file: bool = False
    supports_add_dir: bool = False
    supports_dangerously_skip_permissions: bool = False
    supports_streaming: bool = False
    supports_tool_events: bool = False
    model: str | None = None
    authenticated: bool = False
    warnings: list[str] = Field(default_factory=list)
    raw_help: str | None = None


# ---------------------------------------------------------------------------
# Bridge request / response — the wire contract used by the bridge CLI and
# every MCP tool that drives a backend.
# ---------------------------------------------------------------------------


class BridgeRequest(BaseModel):
    """Inputs to a single bridge invocation."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    cwd: str = "."
    session_id: str | None = None
    model: str | None = None
    sandbox: bool = False
    mode: Mode = "ask"
    return_all_messages: bool = False
    timeout: int = 900
    detach: bool = False
    allow_write: bool = False
    # ``None`` means "use the value from config (env / config.toml)".
    worktree: bool | None = None
    max_output_chars: int = 60_000
    debug: bool = False
    dry_run: bool = False
    backend: BackendName = "auto"
    output_protocol: OutputProtocol = "claude"
    extra_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("prompt")
    @classmethod
    def _prompt_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("prompt must not be empty")
        return value

    @field_validator("timeout")
    @classmethod
    def _timeout_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("timeout must be positive seconds")
        return value

    @field_validator("max_output_chars")
    @classmethod
    def _max_output_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("max_output_chars must be positive")
        return value


class AdapterMetadata(BaseModel):
    """Adapter-side metadata included in every BridgeResponse."""

    model_config = ConfigDict(extra="allow")

    backend: BackendName | None = None
    bin_path: str | None = None
    version: str | None = None
    model: str | None = None
    output_protocol: OutputProtocol | None = None
    supports_streaming: bool = False
    supports_tool_events: bool = False


class BridgeResponse(BaseModel):
    """Stable result envelope returned by the bridge CLI and MCP tools."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    SESSION_ID: str = ""
    job_id: str | None = None
    status: JobStatus = "unknown"
    agent_messages: str | list[dict[str, Any]] = ""
    all_messages: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    # ``error`` is reserved for failures (success=False). Non-fatal advisory
    # text — fallback notices, route warnings, capability warnings — must use
    # ``warnings`` instead so consumers can keep ``if resp.error: retry``
    # semantics. See Phase 3 review (P0).
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    cwd: str = ""
    adapter: AdapterMetadata = Field(default_factory=AdapterMetadata)
    # ``command_preview`` is only emitted when caller asked for both
    # debug=True and dry_run=True; even then, secrets must be redacted by
    # the caller before serialisation. See docs/review-followups.md.
    command_preview: list[str] | None = None
    log_path: str | None = None
    created_at: str = Field(default_factory=lambda: _iso_now())
    updated_at: str = Field(default_factory=lambda: _iso_now())

    def touch(self) -> "BridgeResponse":
        self.updated_at = _iso_now()
        return self


# ---------------------------------------------------------------------------
# Internal canonical event envelope used between adapter and protocol layer
# ---------------------------------------------------------------------------


class CanonicalEvent(BaseModel):
    """Adapter-emitted event before protocol translation.

    Designed so the same in-memory event can be cleanly mapped to Claude
    Code stream-json, OpenAI Codex exec-json, or returned raw.
    """

    model_config = ConfigDict(extra="allow")

    type: Literal[
        "system",
        "user",
        "assistant",
        "tool_use",
        "tool_result",
        "result",
        "error",
        "subagent_event",
    ]
    subtype: str | None = None
    session_id: str | None = None
    role: str | None = None
    text: str | None = None
    content: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] | None = None
    ts: str = Field(default_factory=lambda: _iso_now())


# ---------------------------------------------------------------------------
# Long-task / supervisor records
# ---------------------------------------------------------------------------


class JobRecord(BaseModel):
    """Persisted state of a long-running agy job (one per SESSION_ID/job_id)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    session_id: str | None = None
    status: JobStatus = "running"
    backend: BackendName | None = None
    cwd: str = ""
    pid: int | None = None
    started_at: str = Field(default_factory=lambda: _iso_now())
    updated_at: str = Field(default_factory=lambda: _iso_now())
    finished_at: str | None = None
    exit_code: int | None = None
    log_path: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    events_path: str | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    request: dict[str, Any] = Field(default_factory=dict)
    last_event_at: str | None = None
    error: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def touch(self, *, status: JobStatus | None = None) -> "JobRecord":
        self.updated_at = _iso_now()
        if status is not None:
            self.status = status
        return self


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "AdapterMetadata",
    "BackendName",
    "BridgeRequest",
    "BridgeResponse",
    "CanonicalEvent",
    "Capability",
    "JobRecord",
    "JobStatus",
    "Mode",
    "OutputProtocol",
]
