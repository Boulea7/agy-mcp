"""Tests for agy_mcp.models — pydantic round-trip + validation guards."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agy_mcp.models import (
    AdapterMetadata,
    BridgeRequest,
    BridgeResponse,
    CanonicalEvent,
    Capability,
    JobRecord,
)


# ---------------------------------------------------------------------------
# BridgeRequest
# ---------------------------------------------------------------------------


def test_bridge_request_defaults_are_safe():
    req = BridgeRequest(prompt="hello")
    assert req.mode == "ask"
    assert req.allow_write is False
    assert req.sandbox is False
    assert req.worktree is None  # signals "use config default"
    assert req.backend == "auto"
    assert req.output_protocol == "claude"
    assert req.timeout == 900
    assert req.max_output_chars == 60_000


def test_bridge_request_rejects_empty_prompt():
    with pytest.raises(ValidationError):
        BridgeRequest(prompt="")
    with pytest.raises(ValidationError):
        BridgeRequest(prompt="   \n\t")


def test_bridge_request_rejects_non_positive_timeout():
    with pytest.raises(ValidationError):
        BridgeRequest(prompt="x", timeout=0)
    with pytest.raises(ValidationError):
        BridgeRequest(prompt="x", timeout=-1)


def test_bridge_request_rejects_unknown_field():
    with pytest.raises(ValidationError):
        BridgeRequest(prompt="x", foo="bar")  # type: ignore[call-arg]


def test_bridge_request_rejects_invalid_mode():
    with pytest.raises(ValidationError):
        BridgeRequest(prompt="x", mode="banana")  # type: ignore[arg-type]


def test_bridge_request_rejects_invalid_max_output_chars():
    with pytest.raises(ValidationError):
        BridgeRequest(prompt="x", max_output_chars=0)


# ---------------------------------------------------------------------------
# BridgeResponse
# ---------------------------------------------------------------------------


def test_bridge_response_round_trip():
    resp = BridgeResponse(
        success=True,
        SESSION_ID="conv-123",
        status="completed",
        agent_messages="hi",
    )
    payload = resp.model_dump_json()
    decoded = json.loads(payload)
    assert decoded["SESSION_ID"] == "conv-123"
    assert decoded["status"] == "completed"
    assert decoded["agent_messages"] == "hi"
    assert decoded["adapter"]["backend"] is None


def test_bridge_response_touch_updates_timestamp():
    resp = BridgeResponse(success=False, error="x")
    original = resp.updated_at
    # Sleep a bit so the timestamp string can change (1s resolution).
    import time

    time.sleep(1.1)
    resp.touch()
    assert resp.updated_at >= original


def test_bridge_response_failure_envelope_has_stable_fields():
    resp = BridgeResponse(success=False, error="boom")
    blob = resp.model_dump()
    for key in (
        "success",
        "SESSION_ID",
        "status",
        "agent_messages",
        "all_messages",
        "artifacts",
        "error",
        "cwd",
        "adapter",
        "command_preview",
        "log_path",
        "created_at",
        "updated_at",
    ):
        assert key in blob


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


def test_capability_round_trip():
    cap = Capability(
        bin_path="/usr/local/bin/agy",
        backend="agy",
        version="1.0.0",
        supports_print=True,
        supports_print_timeout=True,
        supports_conversation=True,
        supports_continue=True,
        supports_sandbox=True,
        supports_log_file=True,
        supports_add_dir=True,
        supports_dangerously_skip_permissions=True,
        supports_streaming=False,
        supports_tool_events=False,
        model="Gemini 3.5 Flash",
        authenticated=True,
        warnings=["no streaming output"],
    )
    decoded = Capability.model_validate_json(cap.model_dump_json())
    assert decoded.supports_streaming is False
    assert decoded.warnings == ["no streaming output"]


def test_capability_rejects_unknown_field():
    with pytest.raises(ValidationError):
        Capability(bin_path="x", backend="agy", foo="bar")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# CanonicalEvent
# ---------------------------------------------------------------------------


def test_canonical_event_minimal():
    evt = CanonicalEvent(type="assistant", text="hello")
    assert evt.ts.endswith("Z")
    payload = evt.model_dump(exclude_none=True)
    assert payload["type"] == "assistant"
    assert payload["text"] == "hello"


def test_canonical_event_rejects_bad_type():
    with pytest.raises(ValidationError):
        CanonicalEvent(type="banana", text="x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# JobRecord
# ---------------------------------------------------------------------------


def test_job_record_touch_updates_status():
    record = JobRecord(job_id="job_1", session_id=None)
    record.touch(status="completed")
    assert record.status == "completed"


def test_adapter_metadata_allows_extra_fields():
    meta = AdapterMetadata(backend="agy", custom_field="x")  # type: ignore[call-arg]
    blob = meta.model_dump()
    assert blob["custom_field"] == "x"
