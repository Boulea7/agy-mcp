"""Thin forwarder so the Claude skill can shell out to agy-bridge.

The skill is checked into Claude's user-scope skill directory and runs
in whatever Python the host environment provides. We don't ship the
agy-mcp package alongside the skill; instead we prefer ``uvx`` which
installs the wheel on demand from the user's pinned source.

Selection order:
  1. ``AGY_BRIDGE_CMD`` env var (full shell command, advanced override —
     **trust boundary**, see references/security.md).
  2. ``python -m agy_mcp.bridge`` if importable from the current env.
  3. ``uvx --from agy-mcp==0.1.2 agy-bridge`` as the last-resort
     install-on-demand fallback. Use a fixed package version here;
     branch refs are mutable and not acceptable for the skill launcher.

All argv is forwarded verbatim. We do NOT parse the bridge response —
the caller (the Claude agent) reads the JSON line and decides what to do.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys

BRIDGE_PACKAGE_SPEC = "agy-mcp==0.1.2"


def _has_module(name: str) -> bool:
    """Return True iff ``name`` is importable in the current interpreter."""

    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001 - import failure is a vote against
        return False


def _select_command(argv: list[str]) -> list[str]:
    """Build the argv list for the bridge invocation."""

    override = os.environ.get("AGY_BRIDGE_CMD")
    if override:
        return shlex.split(override) + argv

    # Prefer in-process invocation when agy_mcp is on PYTHONPATH.
    if _has_module("agy_mcp"):
        return [sys.executable, "-m", "agy_mcp.bridge", *argv]

    # Fall back to uvx if available — installs a fixed agy-mcp release on demand.
    if shutil.which("uvx"):
        return [
            "uvx",
            "--from",
            BRIDGE_PACKAGE_SPEC,
            "agy-bridge",
            *argv,
        ]

    # Last resort: ask the user to install.
    err = {
        "success": False,
        "error": (
            "agy-bridge not found. Install `uv` first "
            "(https://docs.astral.sh/uv/getting-started/installation/), "
            f"then `uv tool install {BRIDGE_PACKAGE_SPEC}`, "
            "or set AGY_BRIDGE_CMD to a full shell command."
        ),
    }
    print(json.dumps(err, ensure_ascii=False))
    sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    """Forward argv to agy-bridge and propagate its exit code."""

    cmd = _select_command(list(argv if argv is not None else sys.argv[1:]))
    try:
        completed = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=False,
        )
    except FileNotFoundError as exc:
        err = {"success": False, "error": f"agy-bridge launcher not found: {exc}"}
        print(json.dumps(err, ensure_ascii=False))
        return 127
    return completed.returncode


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
