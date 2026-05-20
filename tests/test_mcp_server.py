"""Tests for the FastMCP server wiring (``agy_mcp.server``)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from agy_mcp import server
from agy_mcp.adapters.base import AdapterRunResult, BaseAdapter, EventSink
from agy_mcp.config import BackendConfig, Config, ExecuteConfig, SafetyConfig
from agy_mcp.models import (
    AdapterMetadata,
    BackendName,
    BridgeRequest,
    BridgeResponse,
    CanonicalEvent,
    Capability,
)
from agy_mcp.safety import SafetyPolicy
from agy_mcp.session_store import SessionStore
from agy_mcp.supervisor import Supervisor
from agy_mcp.utils import is_windows


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
    # Phase 9: tools now return typed pydantic envelopes (FastMCP
    # structuredContent). ``adapter`` is the AdapterMetadata model, not a
    # raw dict — model_dump() round-trips it back to a dict for the wire.
    adapter = out["adapter"]
    assert adapter.bin_path
    assert isinstance(adapter.model_dump(), dict)
    assert out["command_preview"] is not None


def test_mcp_call_tool_redacts_adapter_bin_path_in_structured_content(
    reset_state, monkeypatch, tmp_path: Path,
):
    raw_bin_path = str(Path.home() / ".local" / "bin" / "agy")

    def _fake_bridge_run(request, config, safety):
        redacted_bin_path = safety.redact(raw_bin_path)
        return BridgeResponse(
            success=True,
            status="completed",
            agent_messages="hello",
            SESSION_ID=request.session_id or "",
            cwd=safety.redact(request.cwd),
            adapter=AdapterMetadata(
                backend="agy",
                bin_path=redacted_bin_path,
                version="1.0.0",
                output_protocol=request.output_protocol,
            ),
            command_preview=[redacted_bin_path, "--print", "hello"],
        )

    monkeypatch.setattr(server, "_bridge_run", _fake_bridge_run)

    content, structured = _run_async(
        server.mcp.call_tool(
            "agy",
            {
                "PROMPT": "hello",
                "cd": str(tmp_path),
                "dry_run": True,
                "debug": True,
            },
        )
    )

    text_fallback = "\n".join(getattr(item, "text", "") for item in content)
    structured_blob = json.dumps(structured)
    assert raw_bin_path not in text_fallback
    assert raw_bin_path not in structured_blob
    assert structured["adapter"]["bin_path"] == "~/.local/bin/agy"


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


def test_agy_read_unknown_returns_structured_failure(reset_state):
    out = server.agy_read_tool("job_does_not_exist_12345")
    assert out["success"] is False
    assert "not found" in (out["error"] or "")


def test_agy_read_rejects_invalid_job_id_without_echo(reset_state):
    raw = "job\twith\nctrlbytes"
    out = server.agy_read_tool(raw)
    payload = json.dumps(out.model_dump(mode="json"))
    assert out["success"] is False
    assert out["job_id"] is None
    assert raw not in payload
    assert "ctrlbytes" not in payload


@pytest.mark.parametrize(
    "tool_name",
    ["agy_start", "agy_status", "agy_read", "agy_cancel"],
)
def test_job_id_rejects_secret_shaped_value_without_echo(
    reset_state, tmp_path: Path, tool_name: str
):
    secret_job_id = "job_sk" "-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    if tool_name == "agy_start":
        out = server.agy_start_tool(
            PROMPT="hi",
            cd=str(tmp_path),
            job_id=secret_job_id,
        )
    elif tool_name == "agy_status":
        out = server.agy_status_tool(secret_job_id)
    elif tool_name == "agy_read":
        out = server.agy_read_tool(secret_job_id)
    else:
        out = server.agy_cancel_tool(secret_job_id)

    payload = json.dumps(out.model_dump(mode="json"))
    assert out["success"] is False
    assert secret_job_id not in payload
    assert "job_id" in (out["error"] or "")
    assert out.get("job_id") is None


def test_agy_read_rejects_negative_since(reset_state):
    out = server.agy_read_tool("job_does_not_exist_12345", since=-1)
    assert out["success"] is False
    assert "since" in (out["error"] or "")


def test_agy_cancel_unknown_job_signalled_false(reset_state):
    out = server.agy_cancel_tool("job_does_not_exist_67890")
    assert out["success"] is True
    assert out["signalled"] is False


def test_agy_cancel_rejects_invalid_job_id_without_echo(reset_state):
    raw = "job\twith\nctrlbytes"
    out = server.agy_cancel_tool(raw)
    payload = json.dumps(out.model_dump(mode="json"))
    assert out["success"] is False
    assert out["job_id"] is None
    assert raw not in payload
    assert "ctrlbytes" not in payload


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


def test_agy_sessions_rejects_negative_limit(reset_state):
    out = server.agy_sessions_tool(limit=-1)
    assert out["success"] is False
    assert "limit" in (out["error"] or "")


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
    skill_root = project / ".claude" / "skills" / "collaborating-with-antigravity"
    skill_file = skill_root / "SKILL.md"
    assert skill_file.is_file()
    body = skill_file.read_text(encoding="utf-8")
    assert "collaborating-with-antigravity" in body
    # Phase 7: the full bundle lands — scripts/ + references/, not just
    # a SKILL.md placeholder.
    assert (skill_root / "scripts" / "agy_bridge.py").is_file()
    assert (skill_root / "references" / "usage.md").is_file()
    assert (skill_root / "references" / "prompt-patterns.md").is_file()
    assert (skill_root / "references" / "security.md").is_file()
    # Every file is recorded in the envelope.
    paths = {entry["path"] for entry in installed}
    assert any("SKILL.md" in p for p in paths)
    assert any("agy_bridge.py" in p for p in paths)


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


def test_agy_install_skill_writes_user_scope_antigravity(reset_state, monkeypatch, tmp_path: Path):
    """Phase 7: user-scope antigravity lands under ``~/.agy/skills/``.

    Use a fake HOME so we do not write into the real user's home
    directory during tests.
    """

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Importlib has already resolved ``Path.home()`` at module import; we
    # re-resolve via reload of install._USER_SKILL_DIRS to honour the
    # monkeypatched HOME.
    from agy_mcp import install as install_mod

    fresh_dirs = {
        "claude": fake_home / ".claude" / "skills",
        "codex": fake_home / ".agents" / "skills",
        "antigravity": fake_home / ".agy" / "skills",
    }
    monkeypatch.setattr(install_mod, "_USER_SKILL_DIRS", fresh_dirs)

    out = server.agy_install_skill_tool(targets=["antigravity"], scope="user")
    assert out["success"] is True
    skill = fake_home / ".agy" / "skills" / "agy-collaboration" / "SKILL.md"
    assert skill.is_file()
    body = skill.read_text(encoding="utf-8")
    assert "agy-collaboration" in body


def test_agy_install_skill_all_includes_antigravity(reset_state, monkeypatch, tmp_path: Path):
    """Phase 7: ``all`` expands to all three targets now that antigravity
    has a wrapper-owned destination (``~/.agy/skills/``)."""

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    from agy_mcp import install as install_mod

    fresh_dirs = {
        "claude": fake_home / ".claude" / "skills",
        "codex": fake_home / ".agents" / "skills",
        "antigravity": fake_home / ".agy" / "skills",
    }
    monkeypatch.setattr(install_mod, "_USER_SKILL_DIRS", fresh_dirs)

    out = server.agy_install_skill_tool(targets=["all"], scope="user")
    assert out["success"] is True
    targets = {entry["target"] for entry in out["installed"]}
    assert {"claude", "codex", "antigravity"} <= targets


def test_agy_status_unknown_uses_structured_failure(reset_state):
    """Phase 5 R1 arch P1.3: not-found surfaces in the standard envelope."""

    out = server.agy_status_tool("job_does_not_exist_consistent_envelope")
    assert out["success"] is False
    assert "not found" in (out["error"] or "")
    # Phase 9: agy_status returns ``StatusToolResponse``, not BridgeResponse.
    # The envelope intentionally does NOT carry ``cwd`` — that field
    # belonged to the bridge call, not the metadata tool. ``error`` and
    # ``record`` remain the two canonical wrapper fields.
    assert "error" in out
    assert "record" in out
    assert out["record"] is None


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


def test_agy_install_skill_force_false_is_idempotent(reset_state, tmp_path: Path):
    """Phase 7: re-installing an unchanged bundle reports ``overwrote=False``
    for every file (no needless write)."""

    from agy_mcp.install import install_skills

    project = tmp_path / "proj"
    project.mkdir()
    r1 = install_skills(
        targets=["claude"], scope="project", project_root=project,
    )
    assert r1.success is True
    # First install: nothing existed yet, every file is a fresh write.
    assert all(e.overwrote is False for e in r1.installed)

    r2 = install_skills(
        targets=["claude"], scope="project", project_root=project,
    )
    assert r2.success is True
    # Second install with default ``force=False``: bodies match, so we
    # skip every file and still record overwrote=False.
    assert r2.installed
    assert all(e.overwrote is False for e in r2.installed)


@pytest.mark.skipif(is_windows(), reason="symlink privileges vary on Windows")
def test_agy_install_skill_replaces_matching_leaf_symlink(reset_state, tmp_path: Path):
    from agy_mcp.install import _read_packaged_file, install_skills

    project = tmp_path / "proj"
    skill_root = project / ".claude" / "skills" / "collaborating-with-antigravity"
    skill_root.mkdir(parents=True)
    body = _read_packaged_file("claude", "SKILL.md")
    outside = tmp_path / "outside-skill.md"
    outside.write_text(body, encoding="utf-8")
    dest = skill_root / "SKILL.md"
    dest.symlink_to(outside)

    result = install_skills(
        targets=["claude"], scope="project", project_root=project,
    )

    assert result.success is True
    assert dest.is_file()
    assert not dest.is_symlink()
    assert dest.read_text(encoding="utf-8") == body
    assert outside.read_text(encoding="utf-8") == body
    installed = {entry.path: entry.overwrote for entry in result.installed}
    assert any(path.endswith("SKILL.md") and overwrote for path, overwrote in installed.items())


def test_agy_install_skill_force_true_rewrites_unchanged(reset_state, tmp_path: Path):
    """Phase 7: ``force=True`` rewrites even when the on-disk body matches."""

    from agy_mcp.install import install_skills

    project = tmp_path / "proj"
    project.mkdir()
    r1 = install_skills(
        targets=["claude"], scope="project", project_root=project,
    )
    assert r1.success is True

    r2 = install_skills(
        targets=["claude"], scope="project", project_root=project, force=True,
    )
    assert r2.success is True
    # Force rewrite: every dest already exists, so overwrote=True for all.
    assert r2.installed
    assert all(e.overwrote is True for e in r2.installed)


def test_agy_install_skill_recovers_from_modified_on_disk(reset_state, tmp_path: Path):
    """Phase 7: if a user edits SKILL.md on disk, default install replaces
    it (content differs) and reports ``overwrote=True``."""

    from agy_mcp.install import install_skills

    project = tmp_path / "proj"
    project.mkdir()
    install_skills(
        targets=["claude"], scope="project", project_root=project,
    )
    skill = project / ".claude" / "skills" / "collaborating-with-antigravity" / "SKILL.md"
    skill.write_text("LOCAL EDIT", encoding="utf-8")
    r = install_skills(
        targets=["claude"], scope="project", project_root=project,
    )
    assert r.success is True
    overwrote = {entry.path: entry.overwrote for entry in r.installed}
    assert any(value is True for value in overwrote.values())
    body = skill.read_text(encoding="utf-8")
    assert "collaborating-with-antigravity" in body
    assert "LOCAL EDIT" not in body


def test_agy_install_skill_tool_passes_force(reset_state, tmp_path: Path):
    """Phase 7 R1 arch P2-3: ``force`` on the MCP tool surface plumbs
    through to ``install_skills`` so callers can recover a corrupted
    on-disk bundle without dropping to the CLI."""

    project = tmp_path / "proj"
    project.mkdir()
    out1 = server.agy_install_skill_tool(
        targets=["claude"], scope="project", project_root=str(project),
    )
    assert out1["success"] is True
    # Re-call with force=False: every entry should be skipped (idempotent).
    out2 = server.agy_install_skill_tool(
        targets=["claude"], scope="project", project_root=str(project),
        force=False,
    )
    assert out2["success"] is True
    assert all(e["overwrote"] is False for e in out2["installed"])
    # Re-call with force=True: every entry is rewritten (overwrote=True).
    out3 = server.agy_install_skill_tool(
        targets=["claude"], scope="project", project_root=str(project),
        force=True,
    )
    assert out3["success"] is True
    assert out3["installed"]
    assert all(e["overwrote"] is True for e in out3["installed"])


def test_agy_install_skill_rejects_symlinked_intermediate(reset_state, tmp_path: Path):
    """Phase 7 R1 arch P3-2 + sec P2-2: a symlinked intermediate
    directory **inside** the validated project root is refused at write
    time by ``safe_write_text(verify_under=…)``'s parent walk.

    The user-supplied ``project_root`` itself is allowed to have system
    symlinks in its ancestry (``/tmp/...``, ``/var/...`` on macOS) —
    that's why ``_validate_project_root`` only checks the leaf and
    relies on ``safe_write_text`` to enforce the actual security
    boundary. The defence the security model cares about is the gap
    between input validation and the file write: an attacker who
    swaps ``<root>/.claude`` for a symlink to ``/etc`` between those
    two events must not be able to land a file outside ``<root>``.
    """

    project = tmp_path / "proj"
    project.mkdir()
    # Pre-create the .claude/skills tree as a symlink pointing to a
    # sibling directory. ``install_skills`` will resolve project_root,
    # then try to write under .claude/skills — the symlinked
    # intermediate must be rejected.
    sibling = tmp_path / "escape-target"
    sibling.mkdir()
    (project / ".claude").mkdir()
    (project / ".claude" / "skills").symlink_to(sibling, target_is_directory=True)

    out = server.agy_install_skill_tool(
        targets=["claude"], scope="project", project_root=str(project),
    )
    # The containment check ``resolved_skill_dir.relative_to(validated_root)``
    # catches this: ``.claude/skills`` resolves to ``escape-target``,
    # which is outside ``project/``. No files land.
    assert out["success"] is False
    assert not out["installed"]
    assert any("escapes" in w.lower() for w in out["warnings"])


def test_agy_install_skill_corrupted_bundle_emits_warning(reset_state, monkeypatch, tmp_path: Path):
    """Phase 7 R1 arch P3-3: cover the ``_read_packaged_file`` failure
    branch — when the package data is missing or unreadable, the
    installer emits a per-file warning and the envelope reports
    ``success=False`` cleanly."""

    from agy_mcp import install as install_mod

    def _explode(target: str, rel_path: str) -> str:
        raise FileNotFoundError(f"simulated missing bundle file {target}/{rel_path}")

    monkeypatch.setattr(install_mod, "_read_packaged_file", _explode)

    project = tmp_path / "proj"
    project.mkdir()
    out = server.agy_install_skill_tool(
        targets=["claude"], scope="project", project_root=str(project),
    )
    assert out["success"] is False
    # Every file failed, no installs landed; warnings carry the per-file
    # detail (one per file in the bundle).
    assert out["installed"] == []
    assert any("missing bundle file" in w for w in out["warnings"])


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

    import threading

    # Wrap the existing adapter so the first job blocks on an event.
    # Without this, the recording adapter is instant and the slot is
    # released before the second start can race the limiter, so the
    # rejection branch is never exercised. (Phase 5 R3 arch P2-4.)
    sup = server._supervisor
    assert sup is not None
    cap = _capability()
    block = threading.Event()
    release_seen = threading.Event()

    class _BlockingAdapter(_RecordingAdapter):
        def run(self, request, *, log_path=None, stdout_path=None,
                stderr_path=None, event_sink=None, cancel_event=None):
            release_seen.set()
            block.wait(timeout=5.0)
            return super().run(
                request,
                log_path=log_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                event_sink=event_sink,
                cancel_event=cancel_event,
            )

    events = [CanonicalEvent(type="system", subtype="init")]
    blocking_adapter = _BlockingAdapter(cap=cap, events=events)
    sup._adapter_factory = lambda req, c, s: (blocking_adapter, [])  # type: ignore[attr-defined]
    sup._max_concurrent_jobs = 1  # type: ignore[attr-defined]
    sup._job_slots = threading.Semaphore(1)  # type: ignore[attr-defined]

    first = server.agy_start_tool(PROMPT="hi", cd=str(tmp_path))
    assert first["success"] is True
    assert release_seen.wait(timeout=2.0), "first job never reached run()"
    second = server.agy_start_tool(PROMPT="hi2", cd=str(tmp_path))
    assert second["success"] is False
    assert "busy" in (second["error"] or "")
    block.set()  # release the first job so the worker can finish
    assert _wait_until(
        lambda: server.agy_status_tool(first["job_id"])["record"]["status"]
        == "completed",
    )


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


def test_bridge_request_extra_env_rejects_reserved_underscore(reset_state, tmp_path: Path):
    """Phase 5 R3 sec P3: refuse the POSIX-reserved env name '_'."""

    out = _run_async(
        server.agy_tool(
            PROMPT="hi",
            cd=str(tmp_path),
            extra_env={"_": "ignored"},
        )
    )
    assert out["success"] is False
    assert "extra_env" in (out["error"] or "")


def test_doctor_check_auth_handles_symlink_credentials(reset_state, tmp_path, monkeypatch):
    """Phase 5 R2/R3 sec: symlinked oauth_creds → warning, no f-string bug."""

    from agy_mcp import doctor as doc_mod

    target = tmp_path / "real_creds.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "oauth_creds.json"
    link.symlink_to(target)
    monkeypatch.setattr(doc_mod, "AGY_OAUTH_CREDS_PATH", link)
    safety = SafetyPolicy()
    check = doc_mod._check_auth(safety)
    assert check.ok is False
    assert check.severity == "warning"
    assert "symlink" in check.detail


def test_doctor_check_auth_handles_non_regular(reset_state, tmp_path, monkeypatch):
    """Phase 5 R3 sec P1: non-regular file path interpolates octal mode."""

    from agy_mcp import doctor as doc_mod

    # A directory satisfies lstat without being a regular file or a symlink
    # (S_ISLNK is False, S_ISREG is False). The branch that previously had
    # the broken f-string runs.
    bogus = tmp_path / "creds_dir"
    bogus.mkdir()
    monkeypatch.setattr(doc_mod, "AGY_OAUTH_CREDS_PATH", bogus)
    safety = SafetyPolicy()
    check = doc_mod._check_auth(safety)
    assert check.ok is False
    assert "regular file" in check.detail
    # Verify the f-string interpolated rather than leaking the literal
    # ``{st.st_mode:o}``.
    assert "{st.st_mode" not in check.detail
    assert "st_mode=0o" in check.detail


def test_agy_status_job_id_pattern_aligned_with_store(reset_state):
    """Phase 5 R3 sec P2: server gate matches session_store regex."""

    # A 96-char id (max allowed pre-R3) should now fail the server gate
    # rather than failing later at create_job inside the supervisor.
    out = server.agy_status_tool("z" * 96)
    assert out["success"] is False
    assert "job_id" in (out["error"] or "")


def test_agy_read_translate_schema_is_anyof_enum_or_null(reset_state):
    """Phase 5 R3 P3.13: lock the agy_read.translate JSON schema shape.

    The MCP tool surface advertises ``translate: OutputProtocol | None``;
    pydantic generates an ``anyOf [{enum}, {null}]`` block on top of the
    ``default: null``. A future pydantic / FastMCP upgrade that
    flattened this back to a bare ``string`` would silently break
    clients that branch on the null-default. Pin the shape with a
    schema-level assertion rather than waiting for an integration
    regression.
    """

    import asyncio

    async def _get_schema() -> dict:
        tools = await server.mcp.list_tools()
        for t in tools:
            if t.name == "agy_read":
                return t.inputSchema
        raise AssertionError("agy_read not registered with MCP server")

    schema = asyncio.run(_get_schema())
    translate = schema["properties"]["translate"]
    assert translate["default"] is None
    assert "anyOf" in translate, (
        "translate field should keep its anyOf [enum, null] shape; "
        "found flattened schema: " + repr(translate)
    )
    branches = translate["anyOf"]
    enum_branch = next(b for b in branches if "enum" in b)
    null_branch = next(b for b in branches if b.get("type") == "null")
    assert set(enum_branch["enum"]) == {"raw", "claude", "codex"}
    assert null_branch == {"type": "null"}


# ---------------------------------------------------------------------------
# Phase 9: structuredContent / typed return regression suite
# ---------------------------------------------------------------------------


def test_all_tools_advertise_output_schema(reset_state):
    """Every registered tool returns a pydantic model so MCP clients see
    ``structuredContent`` + an ``outputSchema`` for type-safe parsing.

    Without this guarantee, FastMCP would emit text-only ``content`` and
    a downstream code-gen pipeline that relies on outputSchema would
    silently degrade to ``object``. Pin every tool by name.
    """

    import asyncio

    async def _get_tools() -> list:
        return await server.mcp.list_tools()

    tools = asyncio.run(_get_tools())
    by_name = {t.name: t for t in tools}
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
    assert expected.issubset(by_name.keys()), (
        f"missing tools: {expected - by_name.keys()}"
    )
    for name in expected:
        out_schema = getattr(by_name[name], "outputSchema", None)
        assert out_schema is not None, f"{name} missing outputSchema"
        assert out_schema.get("type") == "object", (
            f"{name}.outputSchema should be an object schema, got {out_schema}"
        )
        # Every envelope shape pins ``success`` so a generic MCP client can
        # branch on it without knowing the specific tool name.
        props = out_schema.get("properties") or {}
        # Resolve $ref if pydantic emitted one (envelope models declared
        # ``success`` as ``bool`` -> direct property).
        if "success" not in props and "$ref" in out_schema:
            defs = out_schema.get("$defs", {})
            ref_name = out_schema["$ref"].rsplit("/", 1)[-1]
            props = defs.get(ref_name, {}).get("properties", {})
        assert "success" in props, (
            f"{name}.outputSchema is missing the canonical 'success' field: "
            f"{out_schema}"
        )


def test_tool_response_models_support_dict_access(reset_state):
    """The dict-like envelope mixin must expose ``[...]`` / ``in`` / ``.get``
    so legacy callers that did ``out['success']`` keep working alongside the
    new ``out.success`` style.
    """

    from agy_mcp.models import (
        BridgeResponse,
        CancelToolResponse,
        StatusToolResponse,
    )

    r = StatusToolResponse(success=False, error="boom")
    assert r["success"] is False
    assert r["error"] == "boom"
    assert "record" in r
    assert r.get("missing") is None
    assert r.get("missing", "default") == "default"

    c = CancelToolResponse(success=True, job_id="job_x", signalled=True)
    assert c["job_id"] == "job_x"
    assert c.signalled is True
    keys = list(c.keys())
    assert {"success", "error", "job_id", "signalled"} <= set(keys)
    assert list(iter(c)) == keys
    assert dict(c)["job_id"] == "job_x"

    b = BridgeResponse(success=True, cwd="/tmp")
    assert b["cwd"] == "/tmp"
    assert b.success is True
    with pytest.raises(KeyError):
        b["does_not_exist"]


def test_all_tool_output_models_round_trip_json(reset_state):
    """Every MCP tool should advertise an outputSchema whose backing model
    can survive pydantic -> JSON -> pydantic round-trips. This pins the
    structuredContent contract independently of FastMCP's text fallback.
    """

    from agy_mcp.models import (
        BridgeResponse,
        CancelToolResponse,
        DoctorToolResponse,
        InstallSkillToolResponse,
        JobRecord,
        ReadToolResponse,
        SessionsToolResponse,
        StatusToolResponse,
    )

    async def _get_tools() -> list:
        return await server.mcp.list_tools()

    tools = {tool.name: tool for tool in asyncio.run(_get_tools())}
    samples = {
        "agy": BridgeResponse(success=True, SESSION_ID="sess-1"),
        "agy_continue": BridgeResponse(success=True, SESSION_ID="sess-2"),
        "agy_start": BridgeResponse(
            success=True,
            job_id="job_roundtrip",
            status="running",
        ),
        "agy_status": StatusToolResponse(
            success=True,
            record=JobRecord(job_id="job_roundtrip", status="completed"),
        ),
        "agy_read": ReadToolResponse(
            success=True,
            job_id="job_roundtrip",
            events=[{"type": "assistant", "text": "ok"}],
            count=1,
        ),
        "agy_cancel": CancelToolResponse(
            success=True,
            job_id="job_roundtrip",
            signalled=False,
        ),
        "agy_sessions": SessionsToolResponse(
            success=True,
            count=1,
            records=[JobRecord(job_id="job_roundtrip")],
        ),
        "agy_doctor": DoctorToolResponse(
            success=True,
            report={"checks": []},
            version="0.1.0",
        ),
        "agy_install_skill": InstallSkillToolResponse(
            success=True,
            installed=[{"target": "claude", "path": "/tmp/SKILL.md"}],
        ),
    }

    assert set(samples).issubset(tools.keys())
    for name, sample in samples.items():
        out_schema = getattr(tools[name], "outputSchema", None)
        assert out_schema is not None, f"{name} missing outputSchema"
        assert out_schema.get("type") == "object"
        json_blob = sample.model_dump_json()
        restored = sample.__class__.model_validate_json(json_blob)
        assert restored.model_dump(mode="json") == sample.model_dump(mode="json")
