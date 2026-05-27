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
from pathlib import Path, PurePosixPath
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = PROJECT_ROOT / "dist"
SRC_ROOT = PROJECT_ROOT / "src"
SKILL_BODIES_ROOT = SRC_ROOT / "agy_mcp" / "_skill_bodies"
SKILL_BODY_EXCLUDED_COMPONENTS = frozenset({"__pycache__"})
SKILL_BODY_EXCLUDED_SUFFIXES = (".pyc", ".pyo")


def _is_required_skill_body_file(path: Path) -> bool:
    """Return whether a source skill-body file should ship to users."""

    if not path.is_file():
        return False
    if any(component in SKILL_BODY_EXCLUDED_COMPONENTS for component in path.parts):
        return False
    if path.name in {".DS_Store", "Thumbs.db"}:
        return False
    return not path.name.endswith(SKILL_BODY_EXCLUDED_SUFFIXES)


def _skill_body_files_for_sdist(
    root: Path = SKILL_BODIES_ROOT,
    project_root: Path = PROJECT_ROOT,
) -> set[str]:
    """Return every bundled skill file path as it appears in the sdist."""

    if not root.is_dir():
        return set()
    return {
        path.relative_to(project_root).as_posix()
        for path in root.rglob("*")
        if _is_required_skill_body_file(path)
    }


def _skill_body_files_for_wheel(
    root: Path = SKILL_BODIES_ROOT,
    src_root: Path = SRC_ROOT,
) -> set[str]:
    """Return every bundled skill file path as it appears in the wheel."""

    if not root.is_dir():
        return set()
    return {
        path.relative_to(src_root).as_posix()
        for path in root.rglob("*")
        if _is_required_skill_body_file(path)
    }

# Files we positively require in the sdist. Missing any of these is a release
# blocker because skill bundles, public docs, and licence are part of the
# product surface — losing them silently would ship a broken install.
_REQUIRED_SDIST_BASE_FILES: set[str] = {
    "LICENSE",
    "PKG-INFO",
    "README.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "docs/architecture.md",
    "docs/cli-capabilities.md",
    "docs/comparison-with-cli-wrappers.md",
    "docs/examples.md",
    "docs/installation.md",
    "docs/output-strategy.md",
    "docs/README_EN.md",
    "docs/README_JA.md",
    "docs/README_ZH-TW.md",
    "docs/release.md",
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
    "src/agy_mcp/routing.py",
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
}
REQUIRED_SDIST_FILES: set[str] = _REQUIRED_SDIST_BASE_FILES | _skill_body_files_for_sdist()

ALLOWED_SDIST_FILES: set[str] = REQUIRED_SDIST_FILES | {
    # Hatchling includes the root .gitignore in sdists even when public
    # project files are whitelisted. It carries no local state or secrets, so
    # allow it explicitly while keeping other dotdirs and private prompts
    # forbidden below.
    ".gitignore",
    "src/agy_mcp/py.typed",
}

# Files we positively require in the wheel. The wheel is what users install,
# so a missing _skill_bodies entry here means ``agy_install_skill`` would
# crash at runtime with ``FileNotFoundError`` and the user never finds out
# until they invoke the tool. Phase 8 review P3: gate on the wheel surface
# explicitly rather than trusting hatch to mirror the sdist whitelist.
_REQUIRED_WHEEL_BASE_FILES: set[str] = {
    "agy_mcp/__init__.py",
    "agy_mcp/__main__.py",
    "agy_mcp/bridge.py",
    "agy_mcp/cli.py",
    "agy_mcp/config.py",
    "agy_mcp/doctor.py",
    "agy_mcp/install.py",
    "agy_mcp/models.py",
    "agy_mcp/routing.py",
    "agy_mcp/safety.py",
    "agy_mcp/server.py",
    "agy_mcp/session_store.py",
    "agy_mcp/supervisor.py",
    "agy_mcp/utils.py",
    "agy_mcp/worktree.py",
    "agy_mcp/adapters/__init__.py",
    "agy_mcp/adapters/agy.py",
    "agy_mcp/adapters/base.py",
    "agy_mcp/adapters/gemini.py",
    "agy_mcp/adapters/protocol.py",
}
REQUIRED_WHEEL_FILES: set[str] = _REQUIRED_WHEEL_BASE_FILES | _skill_body_files_for_wheel()

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


_DIST_INFO_RE = re.compile(r"^agy_mcp-[^/]+\.dist-info/")


def _check_wheel_metadata(label: str, files: list[ArtifactFile]) -> list[str]:
    """Verify the wheel's dist-info carries the expected control files.

    A wheel without RECORD or METADATA installs (pip is lenient) but
    every subsequent ``pip show``, ``importlib.metadata.version``, and
    ``uv pip list`` either misreports or crashes. The release-gate
    check pulls them up so a broken hatch build never escapes CI.
    Phase 8 review P3.
    """

    problems: list[str] = []
    dist_info_files = [f for f in files if _DIST_INFO_RE.match(f.path)]
    if not dist_info_files:
        problems.append(
            f"[{label}] wheel has no .dist-info/ directory; "
            "did the build run to completion?",
        )
        return problems

    expected_suffixes = ("METADATA", "RECORD", "WHEEL")
    present = {f.path.rsplit("/", 1)[-1] for f in dist_info_files}
    for suffix in expected_suffixes:
        if suffix not in present:
            problems.append(
                f"[{label}] wheel dist-info missing required file: {suffix}",
            )

    metadata = next(
        (f for f in dist_info_files if f.path.endswith("/METADATA")),
        None,
    )
    if metadata is not None:
        body = metadata.data.decode("utf-8", errors="replace")
        if "Name: agy-mcp" not in body:
            problems.append(
                f"[{label}] METADATA missing ``Name: agy-mcp`` header",
            )
        if "Version:" not in body:
            problems.append(
                f"[{label}] METADATA missing ``Version:`` header",
            )

    record = next(
        (f for f in dist_info_files if f.path.endswith("/RECORD")),
        None,
    )
    if record is not None:
        try:
            entries = record.data.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            entries = []
        recorded_paths = {line.split(",", 1)[0] for line in entries if line}
        # Every shipped python file must be listed in RECORD. Skipping
        # the dist-info entries themselves keeps the check honest about
        # the wheel payload rather than the metadata bookkeeping.
        payload_paths = {
            f.path for f in files if not _DIST_INFO_RE.match(f.path)
        }
        missing_from_record = sorted(payload_paths - recorded_paths)
        for path in missing_from_record:
            problems.append(
                f"[{label}] wheel ships {path} but RECORD does not list it",
            )
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
        # Required set: every runtime-critical module + every skill body
        # file. Wheels do not ship docs/, so docs are not in the required
        # set. Forbidden-substring still runs to block secrets / private
        # prompts that might slip in.
        problems.extend(
            _check_files(wheel.name, files, REQUIRED_WHEEL_FILES, allowed=None)
        )
        problems.extend(_check_contents(wheel.name, artifact_files))
        problems.extend(_check_wheel_metadata(wheel.name, artifact_files))

    if problems:
        print("Release artefact audit FAILED:", file=sys.stderr)
        for line in problems:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(f"OK: {len(sdists)} sdist(s) and {len(wheels)} wheel(s) clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
