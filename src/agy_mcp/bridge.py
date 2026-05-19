"""Placeholder bridge CLI. Phase 3 will implement full argparse and adapter dispatch."""

from __future__ import annotations

import json
import sys


def main(argv: list[str] | None = None) -> int:
    """Stub bridge CLI emitting a structured 'not implemented' error.

    The schema mirrors :class:`agy_mcp.models.BridgeResponse` so downstream callers
    can rely on a stable shape even before Phase 3 lands.
    """

    payload = {
        "success": False,
        "SESSION_ID": "",
        "status": "failed",
        "agent_messages": "",
        "all_messages": [],
        "error": "agy-bridge CLI is implemented in Phase 3; see docs/architecture.md.",
    }
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
