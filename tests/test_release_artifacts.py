"""Tests for the release artefact audit helper."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release_artifacts.py"
_SPEC = importlib.util.spec_from_file_location("check_release_artifacts", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
release_audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release_audit)

ALLOWED_SDIST_FILES = release_audit.ALLOWED_SDIST_FILES
REQUIRED_SDIST_FILES = release_audit.REQUIRED_SDIST_FILES
REQUIRED_WHEEL_FILES = release_audit.REQUIRED_WHEEL_FILES
ArtifactFile = release_audit.ArtifactFile
_check_contents = release_audit._check_contents
_check_files = release_audit._check_files
_check_wheel_metadata = release_audit._check_wheel_metadata
_is_required_skill_body_file = release_audit._is_required_skill_body_file
_skill_body_files_for_sdist = release_audit._skill_body_files_for_sdist
_skill_body_files_for_wheel = release_audit._skill_body_files_for_wheel


def test_release_required_sets_include_all_bundled_skill_body_files():
    skill_root = Path(__file__).resolve().parents[1] / "src" / "agy_mcp" / "_skill_bodies"
    assert skill_root.is_dir()

    sdist_skill_files = _skill_body_files_for_sdist()
    wheel_skill_files = _skill_body_files_for_wheel()

    assert sdist_skill_files
    assert wheel_skill_files
    assert sdist_skill_files <= REQUIRED_SDIST_FILES
    assert wheel_skill_files <= REQUIRED_WHEEL_FILES
    assert {
        path.relative_to(Path(__file__).resolve().parents[1]).as_posix()
        for path in skill_root.rglob("*")
        if _is_required_skill_body_file(path)
    } == sdist_skill_files


def test_release_skill_body_scan_fails_when_root_is_missing(tmp_path: Path):
    missing_root = tmp_path / "missing-skill-bodies"

    with pytest.raises(RuntimeError, match="required skill body directory"):
        _skill_body_files_for_sdist(root=missing_root, project_root=tmp_path)
    with pytest.raises(RuntimeError, match="required skill body directory"):
        _skill_body_files_for_wheel(root=missing_root, src_root=tmp_path / "src")


def test_release_skill_body_scan_ignores_untracked_files(tmp_path: Path):
    project_root = tmp_path / "project"
    skill_root = project_root / "src" / "agy_mcp" / "_skill_bodies" / "claude"
    skill_root.mkdir(parents=True)
    tracked = skill_root / "SKILL.md"
    untracked = skill_root / "local-note.md"
    tracked.write_text("# tracked\n", encoding="utf-8")
    untracked.write_text("# local only\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "add", tracked.relative_to(project_root).as_posix()],
        cwd=project_root,
        check=True,
        capture_output=True,
    )

    assert _skill_body_files_for_sdist(
        root=project_root / "src" / "agy_mcp" / "_skill_bodies",
        project_root=project_root,
    ) == {"src/agy_mcp/_skill_bodies/claude/SKILL.md"}
    assert _skill_body_files_for_wheel(
        root=project_root / "src" / "agy_mcp" / "_skill_bodies",
        src_root=project_root / "src",
    ) == {"agy_mcp/_skill_bodies/claude/SKILL.md"}


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


def test_wheel_metadata_check_accepts_valid_dist_info():
    files = [
        ArtifactFile("agy_mcp/__init__.py", b""),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/METADATA",
            b"Metadata-Version: 2.4\nName: agy-mcp\nVersion: 0.1.8\n",
        ),
        ArtifactFile("agy_mcp-0.1.8.dist-info/WHEEL", b"Wheel-Version: 1.0\n"),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/RECORD",
            b"agy_mcp/__init__.py,sha256=abc,1\n",
        ),
    ]

    assert _check_wheel_metadata("agy-mcp.whl", files) == []


def test_wheel_metadata_check_rejects_missing_dist_info_files():
    files = [
        ArtifactFile("agy_mcp/__init__.py", b""),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/METADATA",
            b"Metadata-Version: 2.4\nName: agy-mcp\nVersion: 0.1.8\n",
        ),
    ]

    problems = _check_wheel_metadata("agy-mcp.whl", files)

    assert any("missing required file: RECORD" in problem for problem in problems)
    assert any("missing required file: WHEEL" in problem for problem in problems)


def test_wheel_metadata_check_rejects_payload_missing_from_record():
    files = [
        ArtifactFile("agy_mcp/__init__.py", b""),
        ArtifactFile("agy_mcp/server.py", b""),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/METADATA",
            b"Metadata-Version: 2.4\nName: agy-mcp\nVersion: 0.1.8\n",
        ),
        ArtifactFile("agy_mcp-0.1.8.dist-info/WHEEL", b"Wheel-Version: 1.0\n"),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/RECORD",
            b"agy_mcp/__init__.py,sha256=abc,1\n",
        ),
    ]

    problems = _check_wheel_metadata("agy-mcp.whl", files)

    assert any(
        "wheel ships agy_mcp/server.py but RECORD does not list it" in problem
        for problem in problems
    )
