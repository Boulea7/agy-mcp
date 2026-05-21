"""Backend routing — pick the adapter for a given :class:`BridgeRequest`.

Pulled out of ``bridge.py`` in v0.1.5 so the supervisor (and any future
caller) can route without importing the CLI layer. The bridge keeps thin
forwarders (``_build_adapter`` / ``_select_backend``) as re-exports so
historical imports keep working; new code should depend on
``agy_mcp.routing`` directly.

The selector intentionally returns the resolved adapter even when no
binary is found, so the caller can surface the upstream Capability
warnings (binary missing, OAuth missing, --print absent, ...) through
their own envelope rather than turning a partial probe into a hard
exception.
"""

from __future__ import annotations

from typing import Callable

from agy_mcp.adapters import (
    AgyPrintBackend,
    BaseAdapter,
    GeminiCliBackend,
)
from agy_mcp.config import Config
from agy_mcp.models import BackendName, BridgeRequest
from agy_mcp.safety import SafetyPolicy


def build_adapter(
    backend: BackendName, config: Config, safety: SafetyPolicy,
) -> BaseAdapter:
    """Construct the adapter for ``backend`` honouring config-level bin overrides.

    Raises ``ValueError`` for unknown backend names so callers can
    convert to a structured failure envelope; never crashes on a
    missing binary (that is surfaced as a Capability warning instead).
    """

    if backend == "agy":
        return AgyPrintBackend(bin_override=config.backend.agy_bin, safety=safety)
    if backend == "gemini":
        return GeminiCliBackend(bin_override=config.backend.gemini_bin, safety=safety)
    raise ValueError(f"unknown backend {backend!r}")


def select_backend(
    request: BridgeRequest,
    config: Config,
    safety: SafetyPolicy,
    *,
    builder: Callable[[BackendName, Config, SafetyPolicy], BaseAdapter] | None = None,
) -> tuple[BaseAdapter, list[str]]:
    """Return ``(adapter, warnings)``.

    * Explicit ``backend == "agy"`` / ``"gemini"`` constructs the
      requested adapter and surfaces any unavailability as a warning
      so the caller can decide whether to fail fast or fall through.
    * ``backend == "auto"`` prefers agy; falls back to gemini only if
      agy is unhealthy (binary missing, OAuth missing, or no
      ``--print`` support). gemini is lazy-probed so the healthy-agy
      path does not pay an extra subprocess fork.

    ``builder`` is an injection seam: the bridge module aliases its
    own ``_build_adapter`` symbol so tests that monkeypatch the
    bridge surface continue to take effect even when the call enters
    the canonical routing logic here. Outside of those tests the
    default ``build_adapter`` is used.
    """

    build = builder or build_adapter
    warnings: list[str] = []
    if request.backend in ("agy", "gemini"):
        adapter = build(request.backend, config, safety)
        cap = adapter.detect()
        if not cap.bin_path:
            warnings.append(
                f"requested backend={request.backend!r} not available: "
                + "; ".join(cap.warnings)
            )
        return adapter, warnings

    # auto routing — lazy-probe gemini only when agy is unhealthy. Each
    # build_adapter call re-probes, so unconditional gemini detection in
    # the healthy-agy path is wasted latency (see Phase 3 review P1.2).
    agy = build("agy", config, safety)
    cap_agy = agy.detect()
    if cap_agy.bin_path and cap_agy.authenticated and cap_agy.supports_print:
        return agy, warnings
    gemini = build("gemini", config, safety)
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


__all__ = ["build_adapter", "select_backend"]
