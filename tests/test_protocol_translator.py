"""Tests for ProtocolTranslator — raw / Claude / Codex schemas + redaction."""

from __future__ import annotations

import pytest

from agy_mcp.adapters.protocol import ProtocolTranslator, _redact_dict
from agy_mcp.models import CanonicalEvent
from agy_mcp.safety import SafetyPolicy


# ---------------------------------------------------------------------------
# raw
# ---------------------------------------------------------------------------


def test_raw_returns_canonical_envelope():
    evt = CanonicalEvent(
        type="assistant",
        subtype="text",
        session_id="s-1",
        text="hello world",
        content=[{"type": "text", "text": "hello world"}],
    )
    out = ProtocolTranslator("raw").translate(evt)
    assert out["type"] == "assistant"
    assert out["text"] == "hello world"
    assert out["session_id"] == "s-1"


def test_raw_redacts_bearer_in_text():
    evt = CanonicalEvent(type="error", text="failure: Authorization: Bearer abcdefghijklmnopqrst")
    out = ProtocolTranslator("raw").translate(evt)
    # The token portion gets redacted; ``Bearer `` literal remains so logs
    # remain interpretable.
    assert "abcdefghijklmnopqrst" not in out["text"]
    assert "***" in out["text"]


# ---------------------------------------------------------------------------
# Claude protocol
# ---------------------------------------------------------------------------


def test_claude_system_init_event():
    evt = CanonicalEvent(
        type="system",
        subtype="init",
        session_id="s-1",
        metadata={"backend": "agy", "model": "gemini-3-pro"},
    )
    out = ProtocolTranslator("claude").translate(evt)
    assert out["type"] == "system"
    assert out["subtype"] == "init"
    assert out["session_id"] == "s-1"
    assert out["metadata"]["backend"] == "agy"
    # Has a ts field (carried through from canonical event).
    assert out.get("ts")


def test_claude_assistant_event_carries_content_list():
    evt = CanonicalEvent(
        type="assistant",
        session_id="s-2",
        text="hi",
        content=[{"type": "text", "text": "hi"}],
    )
    out = ProtocolTranslator("claude").translate(evt)
    assert out["type"] == "assistant"
    assert out["session_id"] == "s-2"
    assert out["message"]["role"] == "assistant"
    assert out["message"]["content"] == [{"type": "text", "text": "hi"}]


def test_claude_result_success_event():
    evt = CanonicalEvent(
        type="result",
        subtype="success",
        session_id="s-3",
        text="all good",
        metadata={"duration_ms": 1234, "exit_code": 0},
    )
    out = ProtocolTranslator("claude").translate(evt)
    assert out["type"] == "result"
    assert out["subtype"] == "success"
    assert out["is_error"] is False
    assert out["duration_ms"] == 1234
    assert out["exit_code"] == 0
    assert out["result"] == "all good"


def test_claude_error_translates_to_result_with_error_flag():
    evt = CanonicalEvent(type="error", subtype="auth_timeout", text="timed out")
    out = ProtocolTranslator("claude").translate(evt)
    assert out["type"] == "result"
    assert out["subtype"] == "error_during_execution"
    assert out["is_error"] is True
    assert out["error"] == "timed out"
    assert out["error_subtype"] == "auth_timeout"


def test_claude_handles_unknown_event_type_gracefully():
    # Build event manually to bypass enum guard.
    evt = CanonicalEvent(type="system", subtype="weird_subtype")
    evt.type = "totally_unknown"  # type: ignore[assignment]
    out = ProtocolTranslator("claude").translate(evt)
    assert out["type"] == "system"
    assert out["subtype"] == "debug"
    assert out["metadata"]["original_type"] == "totally_unknown"


def test_claude_tool_use_event_wrapped_under_assistant():
    evt = CanonicalEvent(
        type="tool_use",
        subtype="invoke_subagent",
        session_id="s-9",
        metadata={"name": "sub-1"},
    )
    out = ProtocolTranslator("claude").translate(evt)
    assert out["type"] == "assistant"
    assert out["message"]["content"][0]["type"] == "tool_use"
    assert out["message"]["content"][0]["subtype"] == "invoke_subagent"


# ---------------------------------------------------------------------------
# Codex protocol
# ---------------------------------------------------------------------------


def test_codex_system_init_becomes_thread_started():
    evt = CanonicalEvent(
        type="system", subtype="init", session_id="t-1",
        metadata={"model": "gemini-3-pro"},
    )
    out = ProtocolTranslator("codex").translate(evt)
    assert out["type"] == "thread.started"
    assert out["thread_id"] == "t-1"
    assert out["metadata"]["model"] == "gemini-3-pro"


def test_codex_conversation_started_emits_thread_identified():
    evt = CanonicalEvent(
        type="system", subtype="conversation_started", session_id="t-2",
    )
    out = ProtocolTranslator("codex").translate(evt)
    assert out["type"] == "thread.identified"
    assert out["thread_id"] == "t-2"


def test_codex_assistant_event_becomes_item_completed():
    evt = CanonicalEvent(type="assistant", session_id="t-3", text="hi")
    out = ProtocolTranslator("codex").translate(evt)
    assert out["type"] == "item.completed"
    assert out["thread_id"] == "t-3"
    assert out["item"]["type"] == "agent_message"
    assert out["item"]["text"] == "hi"


def test_codex_result_success_emits_turn_completed():
    evt = CanonicalEvent(
        type="result", subtype="success", session_id="t-4",
        metadata={"duration_ms": 999, "exit_code": 0},
    )
    out = ProtocolTranslator("codex").translate(evt)
    assert out["type"] == "turn.completed"
    assert out["thread_id"] == "t-4"
    assert out["usage"] == {"duration_ms": 999, "exit_code": 0}
    assert out["error"] is None


def test_codex_result_error_emits_turn_failed():
    evt = CanonicalEvent(
        type="result", subtype="error", session_id="t-5", text="broke",
    )
    out = ProtocolTranslator("codex").translate(evt)
    assert out["type"] == "turn.failed"
    assert out["error"]["message"] == "broke"


def test_codex_error_event_becomes_turn_failed():
    evt = CanonicalEvent(type="error", subtype="spawn_failure", text="no exe", session_id="t-6")
    out = ProtocolTranslator("codex").translate(evt)
    assert out["type"] == "turn.failed"
    assert out["error"]["subtype"] == "spawn_failure"
    assert out["error"]["message"] == "no exe"


def test_codex_unknown_event_falls_through_to_item_updated():
    evt = CanonicalEvent(
        type="subagent_event", subtype="tool_invocation",
        session_id="t-7",
        raw={"k": "v"},
    )
    translator = ProtocolTranslator("codex", include_raw=True)
    out = translator.translate(evt)
    assert out["type"] == "item.updated"
    assert out["thread_id"] == "t-7"
    assert out["item"]["type"] == "tool_invocation"
    assert out["item"]["raw"] == {"k": "v"}


def test_codex_include_raw_false_strips_raw():
    evt = CanonicalEvent(
        type="subagent_event", subtype="tool_invocation", raw={"secret_token": "shhh"},
    )
    out = ProtocolTranslator("codex", include_raw=False).translate(evt)
    assert out["item"]["raw"] is None


# ---------------------------------------------------------------------------
# translate_many
# ---------------------------------------------------------------------------


def test_translate_many_returns_list_in_order():
    events = [
        CanonicalEvent(type="system", subtype="init", session_id="s"),
        CanonicalEvent(type="assistant", text="hi", session_id="s"),
        CanonicalEvent(type="result", subtype="success", session_id="s"),
    ]
    out = ProtocolTranslator("claude").translate_many(events)
    assert [d["type"] for d in out] == ["system", "assistant", "result"]


# ---------------------------------------------------------------------------
# Unknown protocol guard
# ---------------------------------------------------------------------------


def test_translator_rejects_unknown_protocol():
    with pytest.raises(ValueError, match="unknown output_protocol"):
        ProtocolTranslator("banana").translate(CanonicalEvent(type="assistant", text="hi"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _redact_dict
# ---------------------------------------------------------------------------


def test_redact_dict_walks_nested_strings():
    payload = {
        "text": "Authorization: Bearer abcdef123456abcdef123456",
        "nested": {
            "token": "eyJabc.def.ghi.jklmno.pqrstu",
            "values": ["AKIA" "IOSFODNN7EXAMPLE", "safe-value"],
        },
    }
    out = _redact_dict(payload, SafetyPolicy())
    assert "abcdef123456abcdef123456" not in out["text"]
    assert out["nested"]["values"][0] == "***"  # AWS AKID redacted
    assert out["nested"]["values"][1] == "safe-value"


def test_redact_dict_caps_recursion_depth():
    # Deeply nested dict shouldn't blow the stack.
    payload = current = {}
    for _ in range(64):
        current["nested"] = {"v": "Authorization: Bearer abcdef123456abcdef123456"}
        current = current["nested"]
    out = _redact_dict(payload, SafetyPolicy())
    # At the top of the chain, redaction was applied.
    assert "abcdef123456abcdef123456" not in out["nested"]["v"]


def test_redact_dict_returns_truncated_marker_past_cap():
    """Phase 2 review: depth >32 must not echo unredacted subtree."""

    payload = current = {}
    for _ in range(60):
        current["nested"] = {"deep_secret": "Authorization: Bearer leakedToken1234567890"}
        current = current["nested"]
    out = _redact_dict(payload, SafetyPolicy())
    # Walk down to the truncation boundary; somewhere we must hit __truncated__.
    cursor = out
    found = False
    for _ in range(64):
        if isinstance(cursor, dict) and cursor.get("__truncated__") is True:
            found = True
            break
        cursor = cursor.get("nested") if isinstance(cursor, dict) else None
        if cursor is None:
            break
    assert found, "expected __truncated__ marker past depth 32"


# ---------------------------------------------------------------------------
# is_error default (review P2)
# ---------------------------------------------------------------------------


def test_claude_result_cancelled_is_not_error():
    """Future ``result/cancelled`` events must not be misread as errors."""

    evt = CanonicalEvent(type="result", subtype="cancelled", session_id="s")
    out = ProtocolTranslator("claude").translate(evt)
    assert out["is_error"] is False


def test_claude_result_partial_is_not_error():
    evt = CanonicalEvent(type="result", subtype="partial", session_id="s")
    out = ProtocolTranslator("claude").translate(evt)
    assert out["is_error"] is False


def test_claude_result_wrapper_timeout_is_error():
    evt = CanonicalEvent(type="result", subtype="wrapper_timeout", session_id="s")
    out = ProtocolTranslator("claude").translate(evt)
    assert out["is_error"] is True
