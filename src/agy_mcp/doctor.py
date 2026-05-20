"""Minimal Phase 5 doctor — probes binaries / auth / session store; never leaks secrets.

The Phase 7 implementation will extend this with skill-directory checks,
permission audits, and detailed remediation tips. For now we expose the
shape the MCP ``agy_doctor`` tool needs: a structured report with a
``healthy`` boolean and per-check entries that callers can render.

Every string in the report runs through ``SafetyPolicy.redact`` so an
operator's ``$HOME``-rooted path never lands in the MCP transcript.
"""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agy_mcp.adapters.agy import AGY_OAUTH_CREDS_PATH, AgyPrintBackend
from agy_mcp.adapters.gemini import GeminiCliBackend
from agy_mcp.config import Config, get_config
from agy_mcp.safety import SafetyPolicy
from agy_mcp.session_store import SessionStore


@dataclass(slots=True)
class DoctorCheck:
    """Single doctor probe result."""

    name: str
    ok: bool
    detail: str
    severity: str = "info"  # info | warning | error

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass(slots=True)
class DoctorReport:
    healthy: bool
    checks: list[DoctorCheck] = field(default_factory=list)
    platform: str = ""
    python_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "platform": self.platform,
            "python_version": self.python_version,
            "checks": [c.to_dict() for c in self.checks],
        }


def run_doctor(
    *,
    config: Config | None = None,
    safety: SafetyPolicy | None = None,
    agy_adapter: AgyPrintBackend | None = None,
    gemini_adapter: GeminiCliBackend | None = None,
    session_store: SessionStore | None = None,
) -> DoctorReport:
    """Run every probe and return a structured report.

    Never raises — a failing probe becomes a ``DoctorCheck`` with
    ``ok=False`` and a redacted detail string. ``agy_adapter``,
    ``gemini_adapter``, and ``session_store`` may be passed in so the
    doctor reuses the MCP server's already-probed singletons instead
    of paying for fresh ``--help`` / ``--version`` subprocess calls
    on every invocation (Phase 5 R1 arch P1.5).
    """

    cfg = config or get_config()
    sft = safety or SafetyPolicy.from_config(cfg)
    checks: list[DoctorCheck] = []

    checks.append(_check_python(sft))
    checks.append(_check_uv(sft))
    checks.extend(_check_backend(agy_adapter or AgyPrintBackend(safety=sft), sft, label="agy"))
    checks.extend(_check_backend(gemini_adapter or GeminiCliBackend(safety=sft), sft, label="gemini"))
    checks.append(_check_auth(sft))
    checks.append(_check_session_store(cfg, sft, store=session_store))

    healthy = all(c.ok or c.severity != "error" for c in checks)
    return DoctorReport(
        healthy=healthy,
        checks=checks,
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        python_version=sys.version.split()[0],
    )


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def _check_python(safety: SafetyPolicy) -> DoctorCheck:
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 11)
    return DoctorCheck(
        name="python",
        ok=ok,
        severity="error" if not ok else "info",
        detail=safety.redact(
            f"detected Python {sys.version.split()[0]}; requires >= 3.11",
        ),
    )


def _check_uv(safety: SafetyPolicy) -> DoctorCheck:
    bin_path = shutil.which("uv")
    if bin_path:
        return DoctorCheck(
            name="uv",
            ok=True,
            detail=safety.redact(f"found at {bin_path}"),
        )
    return DoctorCheck(
        name="uv",
        ok=False,
        severity="warning",
        detail=safety.redact(
            "uv not found on PATH; install via "
            "https://docs.astral.sh/uv/getting-started/installation/"
        ),
    )


def _check_backend(adapter, safety: SafetyPolicy, *, label: str) -> list[DoctorCheck]:
    try:
        cap = adapter.detect()
    except Exception as exc:  # noqa: BLE001
        return [
            DoctorCheck(
                name=f"{label}_binary",
                ok=False,
                severity="error",
                detail=safety.redact(f"capability probe raised: {exc}"),
            )
        ]
    checks: list[DoctorCheck] = []
    if not cap.bin_path:
        checks.append(
            DoctorCheck(
                name=f"{label}_binary",
                ok=False,
                severity="error" if label == "agy" else "warning",
                detail=safety.redact(
                    f"{label!r} not found on PATH; "
                    + (cap.warnings[0] if cap.warnings else "see install guide"),
                ),
            )
        )
        return checks
    checks.append(
        DoctorCheck(
            name=f"{label}_binary",
            ok=True,
            detail=safety.redact(
                f"{label} {cap.version or '<unknown>'} at {cap.bin_path}",
            ),
        )
    )
    for w in cap.warnings:
        checks.append(
            DoctorCheck(
                name=f"{label}_warning",
                ok=False,
                severity="warning",
                detail=safety.redact(w),
            )
        )
    return checks


def _check_auth(safety: SafetyPolicy) -> DoctorCheck:
    if AGY_OAUTH_CREDS_PATH.is_file():
        return DoctorCheck(
            name="auth",
            ok=True,
            detail=safety.redact(
                f"Google OAuth credentials present at {AGY_OAUTH_CREDS_PATH}",
            ),
        )
    return DoctorCheck(
        name="auth",
        ok=False,
        severity="error",
        detail=safety.redact(
            "Google OAuth credentials missing; run `agy login` before "
            "any non-dry-run invocation."
        ),
    )


def _check_session_store(
    config: Config, safety: SafetyPolicy, *, store: SessionStore | None,
) -> DoctorCheck:
    if store is not None:
        return DoctorCheck(
            name="session_store",
            ok=True,
            detail=safety.redact(f"session store at {store.root}"),
        )
    # Fallback: instantiate one for the report. This will mkdir(0o700)
    # the root, which is idempotent with the cold-start path used by
    # the MCP server. (Phase 5 R1 P2: prefer the singleton when
    # available so the doctor probe stays side-effect-free.)
    try:
        fresh = SessionStore(Path(config.session_store_root()).expanduser())
    except Exception as exc:  # noqa: BLE001
        return DoctorCheck(
            name="session_store",
            ok=False,
            severity="error",
            detail=safety.redact(f"session store init failed: {exc}"),
        )
    return DoctorCheck(
        name="session_store",
        ok=True,
        detail=safety.redact(f"session store at {fresh.root}"),
    )


__all__ = ["DoctorCheck", "DoctorReport", "run_doctor"]
