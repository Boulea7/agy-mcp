"""Placeholder skill installer. Phase 7 will implement target / scope handling."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    print(
        "agy-install-skill is implemented in Phase 7; see docs/installation.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
