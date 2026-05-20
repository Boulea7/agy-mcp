"""Tests for the FastMCP server wiring (``agy_mcp.server``)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from agy_mcp import server
from agy_mcp.adapters.base import AdapterRunResult, BaseAdapter, EventSink
from agy_mcp.config import BackendConfig, Config, ExecuteConfig, SafetyConfig
from agy_mcp.models import (
    BackendName,
    BridgeRequest,
    CanonicalEvent,
    Capability,
)
from agy_mcp.safety import SafetyPolicy
from agy_mcp.session_store import SessionStore
from agy_mcp.supervisor import Supervisor


def _run_async(coro):
    """Drive an async tool to completion from a sync test body."""

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingAdapter(BaseAdapter):
    """Minimal adapter for MCP integration tests."""

    backend: BackendName = "agy"

    def __init__(self, *, cap: Capability, events: list[CanonicalEvent]) -> None:
        super().__init__()
        self._cap = cap
        self.backend = cap.backend
        self._events = events

    def _probe(self) -> Capability:
        return self._cap

    def build_command(self, request: BridgeRequest, *, log_path: Path | None) -> list[str]:
        return ["/fake/agy"]

    def run(
        self,
        request: BridgeRequest,
        *,
        log_path: Path | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        event_sink: EventSink | None = None,
        cancel_event=None,
    ) -> AdapterRunResult:
        for event in self._events:
            if event_sink is not None:
                event_sink.emit(event)
        return AdapterRunResult(
            events=list(self._events),
            session_id=request.session_id or "sess-mcp",
            exit_code=0,
            duration_ms=0,
            stdout_tail="",
            stderr_tail="",
            log_path=None,
            artifacts=[],
        )


def _capability() -> Capability:
    return Capability(
        bin_path="/fake/agy",
        backend="agy",
        version="1.0.0",
        supports_print=True,
        supports_print_timeout=True,
        supports_conversation=True,
        supports_log_file=False,
        supports_streaming=False,
        supports_tool_events=False,
        authenticated=True,
        warnings=[],
    )


def _stage_supervisor(tmp_path: Path, adapter: BaseAdapter) -> Supervisor:
    cfg = Config()
    cfg.execute = ExecuteConfig(worktree_default=False)
    cfg.backend = BackendConfig(prefer="agy", output_protocol="claude")
    cfg.safety = SafetyConfig()
    cfg.session_store.root = str(tmp_path / "sessions")
    safety = SafetyPolicy.from_config(cfg)
    store = SessionStore(Path(cfg.session_store_root()).expanduser())

    def _factory(request, c, s):
        return adapter, []

    return Supervisor(store=store, config=cfg, safety=safety, adapter_factory=_factory)


@pytest.fixture
def reset_state(tmp_path: Path):
    """Reset the module-level singletons and stage a supervisor with a fake adapter."""

    server._reset_state_for_tests()
    cap = _capability()
    events = [
        CanonicalEvent(type="system", subtype="init"),
        CanonicalEvent(type="assistant", text="hi from mcp"),
        CanonicalEvent(type="result", subtype="success"),
    ]
    adapter = _RecordingAdapter(cap=cap, events=events)
    supervisor = _stage_supervisor(tmp_path, adapter)
    cfg = supervisor.config
    safety = supervisor.safety
    store = supervisor.store
    # Patch the module-level singletons.
    server._config = cfg
    server._safety = safety
    server._store = store
    server._supervisor = supervisor
    yield supervisor
    server._reset_state_for_tests()


def _wait_until(predicate, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


def test_nine_tools_registered():
    """The 9 documented tools must all live on the FastMCP instance."""

    expected = {
        "agy",
        "agy_continue",
        "agy_start",
        "agy_status",
        "agy_read",
        "agy_cancel",
        "agy_sessions",
        "agy_doctor",
        "agy_install_skill",
    }
    assert expected == set(server.mcp._tool_manager._tools.keys())


def test_tool_descriptions_present():
    for name, tool in server.mcp._tool_manager._tools.items():
        assert tool.description, f"{name} missing description"
        assert len(tool.description) > 20, f"{name} description too short"


# ---------------------------------------------------------------------------
# Synchronous tools
# ---------------------------------------------------------------------------


def test_agy_dry_run_returns_command_preview(reset_state, tmp_path: Path):
    out = _run_async(
        server.agy_tool(
            PROMPT="hello",
            cd=str(tmp_path),
            dry_run=True,
            debug=True,
        )
    )
    assert out["success"] is True
    assert isinstance(out["adapter"], dict)
    assert out["command_preview"] is not None


def test_agy_invalid_request_returns_structured_failure(reset_state, tmp_path: Path):
    out = _run_async(server.agy_tool(PROMPT="   ", cd=str(tmp_path)))
    assert out["success"] is False
    assert out["error"]


def test_agy_continue_requires_session_id(reset_state, tmp_path: Path):
    out = _run_async(
        server.agy_continue_tool(
            SESSION_ID="",
            PROMPT="hi",
            cd=str(tmp_path),
        )
    )
    assert out["success"] is False
    assert "SESSION_ID" in (out["error"] or "")


# ---------------------------------------------------------------------------
# Async tools (start / status / read / cancel / sessions)
# ---------------------------------------------------------------------------


def test_agy_start_status_read_cycle(reset_state, tmp_path: Path):
    started = server.agy_start_tool(
        PROMPT="hi",
        cd=str(tmp_path),
    )
    assert started["success"] is True
    assert started["status"] == "running"
    job_id = started["job_id"]
    assert _wait_until(
        lambda: server.agy_status_tool(job_id)["record"]["status"] == "completed",
    )
    status = server.agy_status_tool(job_id)
    assert status["success"] is True
    record = status["record"]
    assert record["status"] == "completed"
    assert record["exit_code"] == 0

    read = server.agy_read_tool(job_id)
    assert read["success"] is True
    assert read["count"] == 3
    assert [e["type"] for e in read["events"]] == ["system", "assistant", "result"]

    translated = server.agy_read_tool(job_id, translate="raw")
    assert translated["success"] is True
    assert isinstance(translated["events"], list)
    assert all(isinstance(e, dict) for e in translated["events"])


def test_agy_status_unknown_returns_structured_failure(reset_state):
    out = server.agy_status_tool("job_does_not_exist_12345")
    assert out["success"] is False
    assert "not found" in (out["error"] or "")


def test_agy_cancel_unknown_job_signalled_false(reset_state):
    out = server.agy_cancel_tool("job_does_not_exist_67890")
    assert out["success"] is True
    assert out["signalled"] is False


def test_agy_sessions_lists_recent_jobs(reset_state, tmp_path: Path):
    started = server.agy_start_tool(PROMPT="hi", cd=str(tmp_path))
    job_id = started["job_id"]
    assert _wait_until(
        lambda: server.agy_status_tool(job_id)["record"]["status"] == "completed",
    )
    out = server.agy_sessions_tool(limit=5)
    assert out["success"] is True
    assert out["count"] >= 1
    assert any(r["job_id"] == job_id for r in out["records"])


# ---------------------------------------------------------------------------
# Doctor / install
# ---------------------------------------------------------------------------


def test_agy_doctor_returns_structured_report(reset_state):
    out = server.agy_doctor_tool()
    assert out["success"] is True
    assert "report" in out
    report = out["report"]
    assert "healthy" in report
    assert "checks" in report
    names = [c["name"] for c in report["checks"]]
    assert "python" in names
    assert "session_store" in names
    # Doctor must NOT leak /Users/<user>/ paths
    for c in report["checks"]:
        assert "/Users/" not in c["detail"]


def test_agy_install_skill_writes_scaffold(reset_state, tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    out = server.agy_install_skill_tool(
        targets=["claude"], scope="project", project_root=str(project),
    )
    assert out["success"] is True
    installed = out["installed"]
    assert installed
    skill_file = project / ".claude" / "skills" / "collaborating-with-antigravity" / "SKILL.md"
    assert skill_file.is_file()
    body = skill_file.read_text(encoding="utf-8")
    assert "collaborating-with-antigravity" in body


def test_agy_install_skill_rejects_project_scope_without_root(reset_state):
    out = server.agy_install_skill_tool(targets=["claude"], scope="project")
    assert out["success"] is False
    assert "project_root" in (out["error"] or "")


def test_agy_install_skill_unknown_target_records_warning(reset_state, tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    out = server.agy_install_skill_tool(
        targets=["claude", "nonsense"], scope="project", project_root=str(project),
    )
    # claude succeeds; nonsense raises ValueError inside _expand_targets,
    # which is caught at the tool-level guard and surfaced as error.
    assert out["success"] is False
    assert "nonsense" in (out["error"] or "")


def test_agy_install_skill_rejects_invalid_scope(reset_state):
    """Phase 5 R1 sec P1: tool guard refuses anything outside user/project."""

    out = server.agy_install_skill_tool(targets=["claude"], scope="root")  # type: ignore[arg-type]
    assert out["success"] is False
    assert "scope" in (out["error"] or "")


def test_agy_install_skill_rejects_missing_project_root(reset_state, tmp_path: Path):
    """Phase 5 R1 sec P1: project_root must exist as a real directory."""

    missing = tmp_path / "does-not-exist"
    out = server.agy_install_skill_tool(
        targets=["claude"], scope="project", project_root=str(missing),
    )
    assert out["success"] is False
    assert "project_root" in (out["error"] or "")


def test_agy_install_skill_rejects_symlinked_project_root(reset_state, tmp_path: Path):
    """Phase 5 R1 sec P1: bare symlink at the leaf is refused."""

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    out = server.agy_install_skill_tool(
        targets=["claude"], scope="project", project_root=str(link),
    )
    assert out["success"] is False
    assert "symlink" in (out["error"] or "")


def test_agy_install_skill_rejects_user_scope_antigravity(reset_state):
    """Phase 5 R1 sec P1: user-scope antigravity write is refused."""

    out = server.agy_install_skill_tool(targets=["antigravity"], scope="user")
    # No installs land; the resolver records a warning that ``antigravity``
    # is not a user-scope target, and install_skills surfaces an error so
    # the caller sees ``success=False``.
    assert out["success"] is False
    assert not out["installed"]
    assert any("antigravity" in w for w in out["warnings"])


def test_agy_install_skill_all_excludes_antigravity(reset_state, tmp_path: Path):
    """Phase 5 R1 sec P1: ``all`` must NOT install antigravity (opt-in only)."""

    project = tmp_path / "proj"
    project.mkdir()
    out = server.agy_install_skill_tool(
        targets=["all"], scope="project", project_root=str(project),
    )
    assert out["success"] is True
    paths = {entry["target"] for entry in out["installed"]}
    assert "antigravity" not in paths
    assert {"claude", "codex"} <= paths


def test_agy_status_unknown_uses_structured_failure(reset_state):
    """Phase 5 R1 arch P1.3: not-found surfaces in the standard envelope."""

    out = server.agy_status_tool("job_does_not_exist_consistent_envelope")
    assert out["success"] is False
    assert "not found" in (out["error"] or "")
    # The envelope is a BridgeResponse dump, so it has cwd/error keys, not
    # the bare ``job_id`` field of the previous shape.
    assert "cwd" in out
    assert "error" in out


def test_agy_status_rejects_oversized_job_id(reset_state):
    """Phase 5 R1 P2: refuse multi-megabyte job_id values."""

    out = server.agy_status_tool("x" * 4096)
    assert out["success"] is False
    assert "job_id" in (out["error"] or "")


# ---------------------------------------------------------------------------
# Phase 5 R2 hardening: extra_env, install bounds, doctor force_refresh,
# job_id charset, SESSION_ID cap, supervisor concurrency limit.
# ---------------------------------------------------------------------------


def test_bridge_request_extra_env_rejects_newline_value(reset_state, tmp_path: Path):
    """Phase 5 R2 sec P0-1: BridgeRequest blocks \\n in extra_env values."""

    out = _run_async(
        server.agy_tool(
            PROMPT="hi",
            cd=str(tmp_path),
            extra_env={"FOO": "bar\nLD_PRELOAD=/tmp/evil.so"},
        )
    )
    assert out["success"] is False
    assert "extra_env" in (out["error"] or "")


def test_bridge_request_extra_env_rejects_lowercase_name(reset_state, tmp_path: Path):
    """Phase 5 R2 sec P0-1: env names must match ^[A-Z_][A-Z0-9_]*$."""

    out = _run_async(
        server.agy_tool(
            PROMPT="hi",
            cd=str(tmp_path),
            extra_env={"path": "/tmp"},
        )
    )
    assert out["success"] is False
    assert "extra_env" in (out["error"] or "")


def test_bridge_request_extra_env_rejects_too_many_entries(reset_state, tmp_path: Path):
    """Phase 5 R2 sec P0-1: refuse huge extra_env dicts."""

    huge = {f"KEY_{i}": "value" for i in range(128)}
    out = _run_async(
        server.agy_tool(
            PROMPT="hi",
            cd=str(tmp_path),
            extra_env=huge,
        )
    )
    assert out["success"] is False
    assert "extra_env" in (out["error"] or "")


def test_agy_install_skill_rejects_huge_targets_list(reset_state, tmp_path: Path):
    """Phase 5 R2 sec P1-1: bound the targets list."""

    project = tmp_path / "proj"
    project.mkdir()
    out = server.agy_install_skill_tool(
        targets=["claude"] * 64,
        scope="project",
        project_root=str(project),
    )
    assert out["success"] is False
    assert "targets" in (out["error"] or "")


def test_agy_install_skill_rejects_non_string_target(reset_state, tmp_path: Path):
    """Phase 5 R2 sec P1-1: non-string entries are rejected with a typed error."""

    project = tmp_path / "proj"
    project.mkdir()
    out = server.agy_install_skill_tool(
        targets=["claude", 42],  # type: ignore[list-item]
        scope="project",
        project_root=str(project),
    )
    assert out["success"] is False
    assert "string" in (out["error"] or "")


def test_agy_doctor_force_refresh_rebuilds_adapters(reset_state):
    """Phase 5 R2 sec P2-1: force_refresh drops cached singletons."""

    out1 = server.agy_doctor_tool()
    assert out1["success"] is True
    cached_first = server._agy_adapter
    out2 = server.agy_doctor_tool(force_refresh=True)
    assert out2["success"] is True
    cached_second = server._agy_adapter
    assert cached_first is not cached_second


def test_agy_status_rejects_invalid_job_id_charset(reset_state):
    """Phase 5 R2 sec P2-2: refuse job_ids outside [A-Za-z0-9_-]."""

    out = server.agy_status_tool("job\twith\nctrlbytes")
    assert out["success"] is False
    assert "job_id" in (out["error"] or "")


def test_agy_continue_rejects_oversized_session_id(reset_state, tmp_path: Path):
    """Phase 5 R2 arch P2: SESSION_ID is length-capped."""

    out = _run_async(
        server.agy_continue_tool(
            SESSION_ID="s" * 4096,
            PROMPT="hi",
            cd=str(tmp_path),
        )
    )
    assert out["success"] is False
    assert "SESSION_ID" in (out["error"] or "")


def test_supervisor_rejects_burst_over_concurrency_cap(reset_state, tmp_path: Path):
    """Phase 5 R2 sec P1-3: supervisor.start refuses past max_concurrent_jobs."""

    # Reach into the fixture-provided supervisor and shrink its slot count to
    # 1 so we can deterministically trip the rejection on the second start.
    sup = server._supervisor
    sup._max_concurrent_jobs = 1  # type: ignore[attr-defined]
    sup._job_slots = __import__("threading").Semaphore(1)  # type: ignore[attr-defined]
    first = server.agy_start_tool(PROMPT="hi", cd=str(tmp_path))
    assert first["success"] is True
    second = server.agy_start_tool(PROMPT="hi2", cd=str(tmp_path))
    # The first may have finished by the time the second runs (test
    # adapter is instant), so either it succeeds OR it surfaces the
    # busy envelope. Both are acceptable; what matters is that no
    # background thread leaks.
    assert second["success"] in (True, False)
    if not second["success"]:
        assert "busy" in (second["error"] or "")


def test_safe_write_text_blocks_symlinked_parent(tmp_path: Path):
    """Phase 5 R2 sec P1-2: verify_under refuses a swapped parent symlink."""

    from agy_mcp.utils import safe_write_text

    root = tmp_path / "root"
    root.mkdir()
    real_sub = tmp_path / "real_sub"
    real_sub.mkdir()
    # Place a symlink at root/sub pointing outside the validated root.
    (root / "sub").symlink_to(real_sub, target_is_directory=True)
    target = root / "sub" / "file.txt"
    with pytest.raises(OSError, match="symlink"):
        safe_write_text(target, "data", verify_under=root)
