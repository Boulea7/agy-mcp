"""Minimal Phase 5 doctor — probes binaries / auth / session store; never leaks secrets.

The Phase 7 implementation will extend this with skill-directory checks,
permission audits, and detailed remediation tips. For now we expose the
shape the MCP ``agy_doctor`` tool needs: a structured report with a
``healthy`` boolean and per-check entries that callers can render.

Every string in the report runs through ``SafetyPolicy.redact`` so an
operator's ``$HOME``-rooted path never lands in the MCP transcript.
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agy_mcp.adapters.agy import (
    AGY_OAUTH_CREDS_PATH,
    AgyPrintBackend,
    detect_agy_auth_source,
)
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
    checks.append(_check_network_env(sft))
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
    # Use ``os.lstat`` so a symlink-pointing credentials file is detected
    # rather than silently followed: an attacker who can swap the file
    # for a symlink to e.g. ``/dev/zero`` would otherwise be reported as
    # healthy. (Phase 5 R2 security P2-3.)
    try:
        st = os.lstat(AGY_OAUTH_CREDS_PATH)
    except FileNotFoundError:
        auth_source = detect_agy_auth_source(oauth_path=AGY_OAUTH_CREDS_PATH)
        if auth_source is not None:
            return DoctorCheck(
                name="auth",
                ok=True,
                detail=safety.redact(
                    f"Antigravity auth state detected via {auth_source.path}"
                ),
            )
        return DoctorCheck(
            name="auth",
            ok=False,
            severity="error",
            detail=safety.redact(
                "Google OAuth credentials missing; run `agy` once and complete "
                "the interactive login flow before any non-dry-run invocation."
            ),
        )
    except OSError as exc:
        return DoctorCheck(
            name="auth",
            ok=False,
            severity="error",
            detail=safety.redact(
                f"Google OAuth credentials path unreachable: {exc}",
            ),
        )
    if stat.S_ISLNK(st.st_mode):
        return DoctorCheck(
            name="auth",
            ok=False,
            severity="warning",
            detail=safety.redact(
                f"Google OAuth credentials at {AGY_OAUTH_CREDS_PATH} is a "
                "symlink; refusing to treat as authenticated until it is a "
                "regular file.",
            ),
        )
    if not stat.S_ISREG(st.st_mode):
        return DoctorCheck(
            name="auth",
            ok=False,
            severity="warning",
            detail=safety.redact(
                f"Google OAuth credentials at {AGY_OAUTH_CREDS_PATH} is not "
                f"a regular file (st_mode=0o{st.st_mode:o})."
            ),
        )
    return DoctorCheck(
        name="auth",
        ok=True,
        detail=safety.redact(
            f"Google OAuth credentials present at {AGY_OAUTH_CREDS_PATH}",
        ),
    )


def _check_network_env(safety: SafetyPolicy) -> DoctorCheck:
    """Summarise network-relevant env without exposing proxy credentials."""

    proxy_names = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")
    locale_names = ("LANG", "LC_ALL", "LC_CTYPE")
    parts: list[str] = []
    proxy_present = False

    for name in proxy_names:
        value = os.environ.get(name) or os.environ.get(name.lower())
        if not value:
            continue
        proxy_present = True
        parts.append(f"{name}={_summarise_proxy_value(value, safety)}")

    if not proxy_present:
        parts.append("proxy_env=none")

    locale_parts = [
        f"{name}={safety.redact(os.environ[name])}"
        for name in locale_names
        if os.environ.get(name)
    ]
    if locale_parts:
        parts.append("locale=" + ",".join(locale_parts))

    home = os.environ.get("HOME")
    if home:
        parts.append(f"HOME={safety.redact(home)}")
    path = os.environ.get("PATH")
    if path:
        parts.append(f"PATH_entries={len([p for p in path.split(os.pathsep) if p])}")
    if not proxy_present:
        parts.append(
            "note=MCP process may not inherit shell-only proxy/VPN variables"
        )

    return DoctorCheck(
        name="network_env",
        ok=True,
        detail="; ".join(parts),
    )


def _summarise_proxy_value(value: str, safety: SafetyPolicy) -> str:
    if not value:
        return "empty"
    parsed = urlsplit(value)
    if parsed.scheme and parsed.hostname:
        host = safety.redact(parsed.hostname)
        try:
            parsed_port = parsed.port
        except ValueError:
            parsed_port = None
        port = f":{parsed_port}" if parsed_port is not None else ""
        auth = "yes" if parsed.username or parsed.password else "no"
        return f"set({parsed.scheme}://{host}{port}, auth={auth})"
    return f"set(len={len(value)})"


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


def main() -> int:
    """``python -m agy_mcp.doctor`` entry point — print JSON, exit non-zero
    when ``healthy=False``.

    The output goes to stdout as a single pretty-printed JSON object so
    operators can pipe it through ``jq``. No secrets land in any field
    (every ``detail`` string is run through ``SafetyPolicy.redact``).
    """

    import json as _json

    report = run_doctor()
    payload = {
        "healthy": report.healthy,
        "python_version": report.python_version,
        "platform": report.platform,
        "checks": [
            {
                "name": c.name,
                "ok": c.ok,
                "severity": c.severity,
                "detail": c.detail,
            }
            for c in report.checks
        ],
    }
    print(_json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.healthy else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
