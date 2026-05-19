"""Adapter package — primary AgyPrintBackend, fallback GeminiCliBackend, plus translator."""

from agy_mcp.adapters.agy import AGY_BINARY_NAME, AGY_OAUTH_CREDS_PATH, AgyPrintBackend
from agy_mcp.adapters.base import (
    AdapterRunResult,
    BaseAdapter,
    EventSink,
    ListEventSink,
    detect_flags,
    has_flag,
)
from agy_mcp.adapters.gemini import GEMINI_BINARY_NAME, GeminiCliBackend
from agy_mcp.adapters.protocol import ProtocolTranslator

__all__ = [
    "AGY_BINARY_NAME",
    "AGY_OAUTH_CREDS_PATH",
    "AdapterRunResult",
    "AgyPrintBackend",
    "BaseAdapter",
    "EventSink",
    "GEMINI_BINARY_NAME",
    "GeminiCliBackend",
    "ListEventSink",
    "ProtocolTranslator",
    "detect_flags",
    "has_flag",
]

