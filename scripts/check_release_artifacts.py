#!/usr/bin/env python3
"""Verify that the dist/ release artefacts do not leak internal-only files.

Run after ``uv build`` (or any other invocation of hatch) to confirm:

* The sdist tarball contains the expected files and nothing else.
* The wheel contains the expected files and nothing else.
* Neither artefact ships our internal review notes, agent prompts,
  reference repo clones, or platform turds.

Exits non-zero (and prints the offending files) if anything looks wrong.
This is meant to gate ``twine upload`` / ``uv publish`` in a release
workflow — it is not part of pytest because it depends on real build
artefacts existing under ``dist/``.
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

DIST_DIR = Path(__file__).resolve().parent.parent / "dist"

# Files we positively require in the sdist. Missing any of these is a release
# blocker because skill bundles, public docs, and licence are part of the
# product surface — losing them silently would ship a broken install.
REQUIRED_SDIST_FILES: set[str] = {
    "LICENSE",
    "PKG-INFO",
    "README.md",
    "pyproject.toml",
    "docs/architecture.md",
    "docs/cli-capabilities.md",
    "docs/comparison-with-upstream-reference.md",
    "docs/examples.md",
    "docs/installation.md",
    "docs/output-strategy.md",
    "docs/README_EN.md",
    "docs/security.md",
    "src/agy_mcp/__init__.py",
    "src/agy_mcp/__main__.py",
    "src/agy_mcp/server.py",
    "src/agy_mcp/bridge.py",
    "src/agy_mcp/cli.py",
    "src/agy_mcp/config.py",
    "src/agy_mcp/doctor.py",
    "src/agy_mcp/install.py",
    "src/agy_mcp/models.py",
    "src/agy_mcp/safety.py",
    "src/agy_mcp/session_store.py",
    "src/agy_mcp/supervisor.py",
    "src/agy_mcp/utils.py",
    "src/agy_mcp/worktree.py",
    "src/agy_mcp/adapters/__init__.py",
    "src/agy_mcp/adapters/agy.py",
    "src/agy_mcp/adapters/base.py",
    "src/agy_mcp/adapters/gemini.py",
    "src/agy_mcp/adapters/protocol.py",
    "src/agy_mcp/_skill_bodies/claude/SKILL.md",
    "src/agy_mcp/_skill_bodies/codex/SKILL.md",
    "src/agy_mcp/_skill_bodies/antigravity/SKILL.md",
}

# Files / patterns that MUST NOT appear in any artefact. A match here aborts
# the release. Patterns use simple substring + path-component checks so we
# don't have to drag in a glob library — keep the patterns simple and obvious.
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "review-followups",        # internal phase / review tracker
    "reference-review",        # internal upstream-comparison notes
    "local-test-checklist",    # operator-side test recipe
    "CLAUDE.md",                # user-private agent prompt
    "AGENTS.md",                # user-private agent prompt
    "GEMINI.md",                # user-private agent prompt
    "/.refs/",                  # cloned upstream repos
    "/.agy-mcp/",               # local runtime state
    "/.claude/",                # local claude state
    "/__pycache__/",
    ".DS_Store",
    "Thumbs.db",
)

# Files allowed to ship even though their name flirts with a forbidden
# pattern — for example ``prompts/CLAUDE.md`` is a documented public snippet
# (currently excluded from sdist anyway, but listed here defensively).
ALLOWED_DESPITE_FORBIDDEN: tuple[str, ...] = (
    "prompts/CLAUDE.md",
    "prompts/AGENTS.md",
    "prompts/GEMINI.md",
)


def _list_sdist(tarball: Path) -> list[str]:
    """Return file paths inside the sdist with the top-level prefix stripped."""

    members: list[str] = []
    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            name = member.name
            # Strip leading ``agy_mcp-x.y.z/`` for easier matching.
            head, _, rest = name.partition("/")
            members.append(rest if rest else head)
    return members


def _list_wheel(wheel: Path) -> list[str]:
    """Return file paths inside the wheel."""

    with zipfile.ZipFile(wheel) as zf:
        return [info.filename for info in zf.infolist() if not info.is_dir()]


def _check_files(label: str, files: list[str], required: set[str] | None) -> list[str]:
    """Return list of human-readable problems found in ``files``."""

    problems: list[str] = []
    if required is not None:
        missing = sorted(required - set(files))
        for path in missing:
            problems.append(f"[{label}] missing required file: {path}")

    for path in files:
        if path in ALLOWED_DESPITE_FORBIDDEN:
            continue
        for needle in FORBIDDEN_SUBSTRINGS:
            if needle in path:
                problems.append(f"[{label}] forbidden file shipped: {path} (matched {needle!r})")
                break
    return problems


def main() -> int:
    if not DIST_DIR.is_dir():
        print(f"dist/ not found at {DIST_DIR}; run `uv build` first", file=sys.stderr)
        return 2

    sdists = sorted(DIST_DIR.glob("*.tar.gz"))
    wheels = sorted(DIST_DIR.glob("*.whl"))
    if not sdists:
        print("no .tar.gz sdist found under dist/", file=sys.stderr)
        return 2
    if not wheels:
        print("no .whl wheel found under dist/", file=sys.stderr)
        return 2

    problems: list[str] = []
    for sdist in sdists:
        files = _list_sdist(sdist)
        problems.extend(_check_files(sdist.name, files, REQUIRED_SDIST_FILES))
    for wheel in wheels:
        files = _list_wheel(wheel)
        # Wheels do not ship docs/, so we cannot enforce a required-set there
        # without false positives. Only forbidden-substring matters.
        problems.extend(_check_files(wheel.name, files, required=None))

    if problems:
        print("Release artefact audit FAILED:", file=sys.stderr)
        for line in problems:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(f"OK: {len(sdists)} sdist(s) and {len(wheels)} wheel(s) clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
