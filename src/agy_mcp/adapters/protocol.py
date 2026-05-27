"""ProtocolTranslator — map CanonicalEvent into the requested wire schema.

Three target protocols are supported:

* ``raw``    — pass-through; emit the canonical envelope as-is.
* ``claude`` — Claude Code stream-json (``type=system|assistant|user|result``).
* ``codex``  — OpenAI Codex exec-json (``thread.started`` / ``item.completed``
  / ``turn.completed``, item wrapped under ``item:{type:...}``).

The translator never mutates the input event; it returns a new dict.
"""

from __future__ import annotations

from typing import Any, Iterable

from agy_mcp.models import CanonicalEvent, OutputProtocol
from agy_mcp.safety import SafetyPolicy


class ProtocolTranslator:
    def __init__(
        self,
        protocol: OutputProtocol = "claude",
        *,
        safety: SafetyPolicy | None = None,
        include_raw: bool = False,
    ) -> None:
        self.protocol = protocol
        self.safety = safety or SafetyPolicy()
        self.include_raw = include_raw

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def translate(self, event: CanonicalEvent) -> dict[str, Any]:
        if self.protocol == "raw":
            return self._to_raw(event)
        if self.protocol == "claude":
            return self._to_claude(event)
        if self.protocol == "codex":
            return self._to_codex(event)
        raise ValueError(f"unknown output_protocol: {self.protocol!r}")

    def translate_many(self, events: Iterable[CanonicalEvent]) -> list[dict[str, Any]]:
        return [self.translate(e) for e in events]

    # ------------------------------------------------------------------
    # Raw / Claude / Codex
    # ------------------------------------------------------------------

    def _to_raw(self, event: CanonicalEvent) -> dict[str, Any]:
        return _redact_dict(event.model_dump(exclude_none=False), self.safety)

    def _to_claude(self, event: CanonicalEvent) -> dict[str, Any]:
        evt_type = event.type
        if evt_type == "system":
            return _redact_dict(
                {
                    "type": "system",
                    "subtype": event.subtype or "init",
                    "session_id": event.session_id,
                    "ts": event.ts,
                    "metadata": event.metadata or {},
                },
                self.safety,
            )
        if evt_type == "assistant":
            text = event.text or ""
            content = event.content or [{"type": "text", "text": text}]
            payload = {
                "type": "assistant",
                "session_id": event.session_id,
                "ts": event.ts,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
            }
            return _redact_dict(payload, self.safety)
        if evt_type == "user":
            return _redact_dict(
                {
                    "type": "user",
                    "session_id": event.session_id,
                    "ts": event.ts,
                    "metadata": event.metadata or {},
                },
                self.safety,
            )
        if evt_type == "result":
            metadata = event.metadata or {}
            # ``is_error`` should default to False; only explicit error
            # subtypes set it. This protects against future ``result``
            # subtypes like ``cancelled`` or ``partial`` being misread as
            # errors by clients.
            error_subtypes = {
                "error",
                "upstream_error",
                "wrapper_timeout",
                "error_during_execution",
            }
            is_error = (event.subtype or "") in error_subtypes
            return _redact_dict(
                {
                    "type": "result",
                    "subtype": event.subtype or "success",
                    "session_id": event.session_id,
                    "ts": event.ts,
                    "is_error": is_error,
                    "result": event.text or "",
                    "duration_ms": metadata.get("duration_ms"),
                    "exit_code": metadata.get("exit_code"),
                    "metadata": metadata,
                },
                self.safety,
            )
        if evt_type == "error":
            return _redact_dict(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "session_id": event.session_id,
                    "ts": event.ts,
                    "is_error": True,
                    "error": event.text or "",
                    "error_subtype": event.subtype,
                    "metadata": event.metadata or {},
                },
                self.safety,
            )
        if evt_type in ("tool_use", "tool_result", "subagent_event"):
            return _redact_dict(
                {
                    "type": "assistant",
                    "session_id": event.session_id,
                    "ts": event.ts,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use" if evt_type != "tool_result" else "tool_result",
                                "subtype": event.subtype,
                                "metadata": event.metadata or {},
                                "raw": event.raw if self.include_raw else None,
                            }
                        ],
                    },
                },
                self.safety,
            )
        # Unknown shape — surface as system/debug so client never crashes.
        return _redact_dict(
            {
                "type": "system",
                "subtype": "debug",
                "session_id": event.session_id,
                "ts": event.ts,
                "metadata": {"original_type": evt_type, "subtype": event.subtype},
            },
            self.safety,
        )

    def _to_codex(self, event: CanonicalEvent) -> dict[str, Any]:
        evt_type = event.type
        if evt_type == "system" and event.subtype == "init":
            return _redact_dict(
                {
                    "type": "thread.started",
                    "thread_id": event.session_id or "",
                    "ts": event.ts,
                    "metadata": event.metadata or {},
                },
                self.safety,
            )
        if evt_type == "system" and event.subtype in (
            "conversation_started",
            "conversation_resumed",
        ):
            return _redact_dict(
                {
                    "type": "thread.identified",
                    "thread_id": event.session_id or "",
                    "ts": event.ts,
                },
                self.safety,
            )
        if evt_type == "system" and event.subtype == "turn_start":
            return _redact_dict(
                {
                    "type": "turn.started",
                    "thread_id": event.session_id or "",
                    "ts": event.ts,
                    "metadata": event.metadata or {},
                },
                self.safety,
            )
        if evt_type == "assistant":
            return _redact_dict(
                {
                    "type": "item.completed",
                    "thread_id": event.session_id or "",
                    "ts": event.ts,
                    "item": {
                        "id": f"msg-{event.ts}",
                        "type": "agent_message",
                        "text": event.text or "",
                    },
                },
                self.safety,
            )
        if evt_type == "result":
            metadata = event.metadata or {}
            return _redact_dict(
                {
                    "type": "turn.completed" if event.subtype == "success" else "turn.failed",
                    "thread_id": event.session_id or "",
                    "ts": event.ts,
                    "usage": metadata,
                    "error": {"message": event.text or ""}
                    if event.subtype != "success"
                    else None,
                },
                self.safety,
            )
        if evt_type == "error":
            return _redact_dict(
                {
                    "type": "turn.failed",
                    "thread_id": event.session_id or "",
                    "ts": event.ts,
                    "error": {
                        "message": event.text or "",
                        "subtype": event.subtype,
                    },
                },
                self.safety,
            )
        # Default: emit as a generic item.updated so client can introspect.
        return _redact_dict(
            {
                "type": "item.updated",
                "thread_id": event.session_id or "",
                "ts": event.ts,
                "item": {
                    "type": event.subtype or evt_type,
                    "raw": event.raw if self.include_raw else None,
                    "metadata": event.metadata or {},
                },
            },
            self.safety,
        )


# ---------------------------------------------------------------------------
# Recursive redaction helper
# ---------------------------------------------------------------------------


def _redact_dict(value: Any, safety: SafetyPolicy, _depth: int = 0) -> Any:
    """Walk a serialisable structure and run safety.redact on every string."""

    if _depth > 32:
        # Truncate deep subtrees instead of returning them unredacted, so
        # secrets nested past the recursion cap cannot slip through.
        return {"__truncated__": True}
    if isinstance(value, str):
        return safety.redact(value)
    if isinstance(value, list):
        return [_redact_dict(v, safety, _depth + 1) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out[k] = _redact_dict(v, safety, _depth + 1)
        return out
    return value


__all__ = ["ProtocolTranslator"]
