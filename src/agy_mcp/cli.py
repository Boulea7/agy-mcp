"""Entry-point shim that starts the FastMCP stdio server."""

from __future__ import annotations

import sys


def main() -> int:
    """Start the agy-mcp MCP stdio server.

    Returns the process exit code; 0 on clean shutdown.
    """

    # Import is deferred so that `python -m agy_mcp.cli --help` style invocations
    # in pre-flight environments do not pay the FastMCP import cost.
    from agy_mcp.server import run

    try:
        run()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 — top-level guard, surfaces to stderr
        print(f"agy-mcp server failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
