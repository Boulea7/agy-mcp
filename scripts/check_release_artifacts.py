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

import re
import sys
import tarfile
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import NamedTuple

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

ALLOWED_SDIST_FILES: set[str] = REQUIRED_SDIST_FILES | {
    # Hatchling includes the root .gitignore in sdists even when public
    # project files are whitelisted. It carries no local state or secrets, so
    # allow it explicitly while keeping other dotdirs and private prompts
    # forbidden below.
    ".gitignore",
    "src/agy_mcp/py.typed",
    "src/agy_mcp/_skill_bodies/antigravity/references/collaboration.md",
    "src/agy_mcp/_skill_bodies/claude/references/prompt-patterns.md",
    "src/agy_mcp/_skill_bodies/claude/references/security.md",
    "src/agy_mcp/_skill_bodies/claude/references/usage.md",
    "src/agy_mcp/_skill_bodies/claude/scripts/agy_bridge.py",
    "src/agy_mcp/_skill_bodies/codex/references/prompt-patterns.md",
    "src/agy_mcp/_skill_bodies/codex/references/security.md",
    "src/agy_mcp/_skill_bodies/codex/references/usage.md",
    "src/agy_mcp/_skill_bodies/codex/scripts/agy_bridge.py",
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
FORBIDDEN_COMPONENTS: frozenset[str] = frozenset({
    ".refs",
    ".agy-mcp",
    ".claude",
    "__pycache__",
})
FORBIDDEN_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "raw macOS home path",
        re.compile(
            rb"/Users/(?!"
            rb"(?:me|user|username|you|your[-_]user|example)(?:/|$)"
            rb")[A-Za-z0-9._-]+(?:/|$)"
        ),
    ),
    (
        "raw Linux home path",
        re.compile(
            rb"/home/(?!"
            rb"(?:me|user|username|you|your[-_]user|example)(?:/|$)"
            rb")[A-Za-z0-9._-]+(?:/|$)"
        ),
    ),
    (
        "raw Windows home path",
        re.compile(
            rb"[A-Za-z]:\\Users\\(?!"
            rb"(?:me|user|username|you|your[-_]user|example)(?:\\|$)"
            rb")[A-Za-z0-9._-]+(?:\\|$)"
        ),
    ),
    (
        "OpenAI-style API key",
        re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    ),
    (
        "AWS access key id",
        re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    (
        "Slack token",
        re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    (
        "JWT token",
        re.compile(
            rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
            rb"[A-Za-z0-9_-]{10,}\b",
        ),
    ),
    (
        "authorization header value",
        re.compile(
            rb"(?i)\b(?:authorization|proxy-authorization)\s*:\s*"
            rb"(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}",
        ),
    ),
)

# Files allowed to ship even though their name flirts with a forbidden
# pattern — for example ``prompts/CLAUDE.md`` is a documented public snippet
# (currently excluded from sdist anyway, but listed here defensively).
ALLOWED_DESPITE_FORBIDDEN: tuple[str, ...] = (
    "prompts/CLAUDE.md",
    "prompts/AGENTS.md",
    "prompts/GEMINI.md",
)


class ArtifactFile(NamedTuple):
    path: str
    data: bytes


def _strip_sdist_prefix(name: str) -> str:
    """Strip leading ``agy_mcp-x.y.z/`` for easier matching."""

    head, _, rest = name.partition("/")
    return rest if rest else head


def _list_sdist(tarball: Path) -> list[str]:
    """Return file paths inside the sdist with the top-level prefix stripped."""

    members: list[str] = []
    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            name = member.name
            members.append(_strip_sdist_prefix(name))
    return members


def _list_wheel(wheel: Path) -> list[str]:
    """Return file paths inside the wheel."""

    with zipfile.ZipFile(wheel) as zf:
        return [info.filename for info in zf.infolist() if not info.is_dir()]


def _read_sdist(tarball: Path) -> list[ArtifactFile]:
    """Return file paths and bytes inside the sdist."""

    files: list[ArtifactFile] = []
    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            files.append(
                ArtifactFile(
                    path=_strip_sdist_prefix(member.name),
                    data=extracted.read(),
                )
            )
    return files


def _read_wheel(wheel: Path) -> list[ArtifactFile]:
    """Return file paths and bytes inside the wheel."""

    files: list[ArtifactFile] = []
    with zipfile.ZipFile(wheel) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            files.append(ArtifactFile(path=info.filename, data=zf.read(info)))
    return files


def _check_files(
    label: str,
    files: list[str],
    required: set[str] | None,
    allowed: set[str] | None = None,
) -> list[str]:
    """Return list of human-readable problems found in ``files``."""

    problems: list[str] = []
    file_set = set(files)
    if required is not None:
        missing = sorted(required - file_set)
        for path in missing:
            problems.append(f"[{label}] missing required file: {path}")
    if allowed is not None:
        unexpected = sorted(file_set - allowed)
        for path in unexpected:
            problems.append(f"[{label}] unexpected file shipped: {path}")

    for path in files:
        if path in ALLOWED_DESPITE_FORBIDDEN:
            continue
        components = PurePosixPath(path).parts
        for component in FORBIDDEN_COMPONENTS:
            if component in components:
                problems.append(
                    f"[{label}] forbidden file shipped: {path} "
                    f"(matched component {component!r})"
                )
                break
        else:
            component_matched = False
            for needle in FORBIDDEN_SUBSTRINGS:
                if needle in path:
                    problems.append(
                        f"[{label}] forbidden file shipped: {path} "
                        f"(matched {needle!r})"
                    )
                    component_matched = True
                    break
            if component_matched:
                continue
    return problems


def _check_contents(label: str, files: list[ArtifactFile]) -> list[str]:
    """Return problems for raw local paths or secret-shaped content."""

    problems: list[str] = []
    for file in files:
        # Release artifacts should be source/text only, but skip likely binary
        # payloads defensively so metadata hashes in future wheels do not
        # produce unreadable snippets.
        if b"\x00" in file.data:
            continue
        for kind, pattern in FORBIDDEN_CONTENT_PATTERNS:
            match = pattern.search(file.data)
            if match is None:
                continue
            snippet = match.group(0)[:80].decode("utf-8", errors="replace")
            problems.append(
                f"[{label}] forbidden content in {file.path}: "
                f"{kind} ({snippet!r})"
            )
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
        artifact_files = _read_sdist(sdist)
        files = [file.path for file in artifact_files]
        problems.extend(
            _check_files(
                sdist.name,
                files,
                REQUIRED_SDIST_FILES,
                allowed=ALLOWED_SDIST_FILES,
            )
        )
        problems.extend(_check_contents(sdist.name, artifact_files))
    for wheel in wheels:
        artifact_files = _read_wheel(wheel)
        files = [file.path for file in artifact_files]
        # Wheels do not ship docs/, so we cannot enforce a required-set there
        # without false positives. Only forbidden-substring matters.
        problems.extend(_check_files(wheel.name, files, required=None))
        problems.extend(_check_contents(wheel.name, artifact_files))

    if problems:
        print("Release artefact audit FAILED:", file=sys.stderr)
        for line in problems:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(f"OK: {len(sdists)} sdist(s) and {len(wheels)} wheel(s) clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
