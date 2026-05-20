"""Tests for the release artefact audit helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release_artifacts.py"
_SPEC = importlib.util.spec_from_file_location("check_release_artifacts", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
release_audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release_audit)

ALLOWED_SDIST_FILES = release_audit.ALLOWED_SDIST_FILES
REQUIRED_SDIST_FILES = release_audit.REQUIRED_SDIST_FILES
ArtifactFile = release_audit.ArtifactFile
_check_contents = release_audit._check_contents
_check_files = release_audit._check_files


def test_release_check_rejects_root_dotdir_leaks():
    problems = _check_files(
        "agy-mcp.tar.gz",
        [".refs/upstream/README.md", ".agy-mcp/state.json", ".claude/config.json"],
        required=set(),
        allowed=set(),
    )

    assert any("matched component '.refs'" in problem for problem in problems)
    assert any("matched component '.agy-mcp'" in problem for problem in problems)
    assert any("matched component '.claude'" in problem for problem in problems)


def test_release_check_rejects_unexpected_sdist_extras():
    files = sorted(REQUIRED_SDIST_FILES | {"docs/internal-roadmap.md"})
    problems = _check_files(
        "agy-mcp.tar.gz",
        files,
        required=REQUIRED_SDIST_FILES,
        allowed=ALLOWED_SDIST_FILES,
    )

    assert any(
        "unexpected file shipped: docs/internal-roadmap.md" in problem
        for problem in problems
    )


def test_release_check_allows_hatchling_root_gitignore():
    files = sorted(REQUIRED_SDIST_FILES | {".gitignore"})
    problems = _check_files(
        "agy-mcp.tar.gz",
        files,
        required=REQUIRED_SDIST_FILES,
        allowed=ALLOWED_SDIST_FILES,
    )

    assert problems == []


def test_release_check_rejects_raw_home_path_content():
    problems = _check_contents(
        "agy-mcp.tar.gz",
        [
            ArtifactFile(
                "docs/security.md",
                b"OAuth file: /Users/ln/.gemini/oauth_creds.json",
            )
        ],
    )

    assert any("raw macOS home path" in problem for problem in problems)


def test_release_check_rejects_secret_shaped_content():
    problems = _check_contents(
        "agy-mcp.tar.gz",
        [
            ArtifactFile(
                "README.md",
                b"OPENAI_API_KEY=" b"sk" b"-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            )
        ],
    )

    assert any("OpenAI-style API key" in problem for problem in problems)


def test_release_check_allows_placeholder_secret_docs():
    problems = _check_contents(
        "agy-mcp.tar.gz",
        [
            ArtifactFile(
                "docs/security.md",
                b"Examples: /Users/me/project, /home/user/project, "
                b"C:\\Users\\example\\project, Authorization: <scheme> <token>; "
                b"JWT-style eyJ...",
            )
        ],
    )

    assert problems == []
