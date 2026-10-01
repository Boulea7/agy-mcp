"""Tests for the release artefact audit helper."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import shutil
import subprocess
import sys
import tarfile
import zipfile
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


def test_release_skill_body_scan_requires_only_tracked_release_files(tmp_path: Path):
    project_root = tmp_path / "project"
    skill_root = project_root / "src" / "agy_mcp" / "_skill_bodies"
    tracked_paths = [
        "claude/SKILL.md",
        "claude/references/nested/prompt.md",
        "claude/scripts/helper.py",
        "claude/__pycache__/helper.pyc",
        "claude/references/cache.pyc",
        "claude/references/cache.pyo",
        "claude/.DS_Store",
        "claude/Thumbs.db",
    ]
    untracked_paths = [
        "claude/local-note.md",
        "claude/references/nested/local.md",
        "claude/.SKILL.md.swp",
    ]
    for relative_path in tracked_paths + untracked_paths:
        path = skill_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(
        [
            "git", "add", "-f", "--",
            *[f"src/agy_mcp/_skill_bodies/{path}" for path in tracked_paths],
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
    )

    assert _skill_body_files_for_sdist(
        root=skill_root,
        project_root=project_root,
    ) == {
        "src/agy_mcp/_skill_bodies/claude/SKILL.md",
        "src/agy_mcp/_skill_bodies/claude/references/nested/prompt.md",
        "src/agy_mcp/_skill_bodies/claude/scripts/helper.py",
    }
    assert _skill_body_files_for_wheel(
        root=skill_root,
        src_root=project_root / "src",
    ) == {
        "agy_mcp/_skill_bodies/claude/SKILL.md",
        "agy_mcp/_skill_bodies/claude/references/nested/prompt.md",
        "agy_mcp/_skill_bodies/claude/scripts/helper.py",
    }


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


@pytest.mark.parametrize("self_row", [b"", b"agy_mcp-0.1.8.dist-info/RECORD,,\n"])
def test_wheel_metadata_check_accepts_complete_record_with_optional_self_row(self_row: bytes):
    files = [
        ArtifactFile("agy_mcp/__init__.py", b""),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/METADATA",
            b"Metadata-Version: 2.4\nName: agy-mcp\nVersion: 0.1.8\n",
        ),
        ArtifactFile("agy_mcp-0.1.8.dist-info/WHEEL", b"Wheel-Version: 1.0\n"),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/RECORD",
            b"agy_mcp/__init__.py,,\n"
            b"agy_mcp-0.1.8.dist-info/METADATA,,\n"
            b"agy_mcp-0.1.8.dist-info/WHEEL,,\n" + self_row,
        ),
    ]

    assert _check_wheel_metadata("agy-mcp.whl", files) == []


@pytest.mark.parametrize(
    ("record_data", "expected_problem"),
    [
        (
            b"agy_mcp/__init__.py,,\nagy_mcp-0.1.8.dist-info/WHEEL,,\n",
            "[agy-mcp.whl] wheel ships agy_mcp-0.1.8.dist-info/METADATA "
            "but RECORD does not list it",
        ),
        (
            b"agy_mcp/__init__.py,,\nagy_mcp-0.1.8.dist-info/METADATA,,\n",
            "[agy-mcp.whl] wheel ships agy_mcp-0.1.8.dist-info/WHEEL "
            "but RECORD does not list it",
        ),
    ],
    ids=["metadata", "wheel"],
)
def test_wheel_metadata_check_rejects_dist_info_missing_from_record(
    record_data: bytes, expected_problem: str
):
    files = [
        ArtifactFile("agy_mcp/__init__.py", b""),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/METADATA",
            b"Metadata-Version: 2.4\nName: agy-mcp\nVersion: 0.1.8\n",
        ),
        ArtifactFile("agy_mcp-0.1.8.dist-info/WHEEL", b"Wheel-Version: 1.0\n"),
        ArtifactFile("agy_mcp-0.1.8.dist-info/RECORD", record_data),
    ]

    assert _check_wheel_metadata("agy-mcp.whl", files) == [expected_problem]


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
            b"agy_mcp/__init__.py,,\n"
            b"agy_mcp-0.1.8.dist-info/METADATA,,\n"
            b"agy_mcp-0.1.8.dist-info/WHEEL,,\n",
        ),
    ]

    problems = _check_wheel_metadata("agy-mcp.whl", files)

    assert problems == [
        "[agy-mcp.whl] wheel ships agy_mcp/server.py but RECORD does not list it"
    ]


def test_wheel_metadata_check_allows_omitting_legacy_signature_rows():
    files = [
        ArtifactFile("agy_mcp/__init__.py", b""),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/METADATA",
            b"Metadata-Version: 2.4\nName: agy-mcp\nVersion: 0.1.8\n",
        ),
        ArtifactFile("agy_mcp-0.1.8.dist-info/WHEEL", b"Wheel-Version: 1.0\n"),
        ArtifactFile("agy_mcp-0.1.8.dist-info/RECORD.jws", b""),
        ArtifactFile("agy_mcp-0.1.8.dist-info/RECORD.p7s", b""),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/RECORD",
            b"agy_mcp/__init__.py,,\n"
            b"agy_mcp-0.1.8.dist-info/METADATA,,\n"
            b"agy_mcp-0.1.8.dist-info/WHEEL,,\n",
        ),
    ]

    assert _check_wheel_metadata("agy-mcp.whl", files) == []


@pytest.mark.parametrize(
    ("extra_path", "expected_problem"),
    [
        (
            "agy_mcp-0.1.8.dist-info/entry_points.txt",
            "[agy-mcp.whl] wheel ships agy_mcp-0.1.8.dist-info/entry_points.txt "
            "but RECORD does not list it",
        ),
        (
            "agy_mcp-0.1.8.dist-info/nested/RECORD.jws",
            "[agy-mcp.whl] wheel ships agy_mcp-0.1.8.dist-info/nested/RECORD.jws "
            "but RECORD does not list it",
        ),
        (
            "agy_mcp/RECORD.p7s",
            "[agy-mcp.whl] wheel ships agy_mcp/RECORD.p7s but RECORD does not list it",
        ),
    ],
)
def test_wheel_metadata_check_requires_rows_for_other_files(
    extra_path: str, expected_problem: str
):
    files = [
        ArtifactFile("agy_mcp/__init__.py", b""),
        ArtifactFile(extra_path, b""),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/METADATA",
            b"Metadata-Version: 2.4\nName: agy-mcp\nVersion: 0.1.8\n",
        ),
        ArtifactFile("agy_mcp-0.1.8.dist-info/WHEEL", b"Wheel-Version: 1.0\n"),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/RECORD",
            b"agy_mcp/__init__.py,,\n"
            b"agy_mcp-0.1.8.dist-info/METADATA,,\n"
            b"agy_mcp-0.1.8.dist-info/WHEEL,,\n",
        ),
    ]

    assert _check_wheel_metadata("agy-mcp.whl", files) == [expected_problem]


def test_wheel_metadata_check_accepts_quoted_record_paths():
    files = [
        ArtifactFile("agy_mcp/data,name.txt", b""),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/METADATA",
            b"Metadata-Version: 2.4\nName: agy-mcp\nVersion: 0.1.8\n",
        ),
        ArtifactFile("agy_mcp-0.1.8.dist-info/WHEEL", b"Wheel-Version: 1.0\n"),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/RECORD",
            b'"agy_mcp/data,name.txt",,\n'
            b"agy_mcp-0.1.8.dist-info/METADATA,,\n"
            b"agy_mcp-0.1.8.dist-info/WHEEL,,\n",
        ),
    ]

    assert _check_wheel_metadata("agy-mcp.whl", files) == []


def test_wheel_metadata_check_reports_malformed_record_csv():
    files = [
        ArtifactFile("agy_mcp/__init__.py", b""),
        ArtifactFile(
            "agy_mcp-0.1.8.dist-info/METADATA",
            b"Metadata-Version: 2.4\nName: agy-mcp\nVersion: 0.1.8\n",
        ),
        ArtifactFile("agy_mcp-0.1.8.dist-info/WHEEL", b"Wheel-Version: 1.0\n"),
        ArtifactFile("agy_mcp-0.1.8.dist-info/RECORD", b'"agy_mcp/__init__.py,,\n'),
    ]

    problems = _check_wheel_metadata("agy-mcp.whl", files)

    assert "[agy-mcp.whl] RECORD is not valid CSV" in problems


@pytest.mark.parametrize(
    ("missing_skill", "expected_code", "expected_stderr"),
    [
        (None, 0, ""),
        (
            "claude/SKILL.md",
            1,
            "Release artefact audit FAILED:\n"
            "  - [agy_mcp-0.1.8.tar.gz] missing required file: "
            "src/agy_mcp/_skill_bodies/claude/SKILL.md\n"
            "  - [agy_mcp-0.1.8-py3-none-any.whl] missing required file: "
            "agy_mcp/_skill_bodies/claude/SKILL.md\n",
        ),
    ],
    ids=["complete", "missing-claude-skill"],
)
def test_release_cli_preserves_required_skills_when_source_and_artifact_are_missing(
    tmp_path: Path, missing_skill: str | None, expected_code: int, expected_stderr: str
):
    project_root = tmp_path / "project"
    script = project_root / "scripts" / "check_release_artifacts.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(_SCRIPT, script)
    package_members = (
        "agy_mcp/__init__.py",
        "agy_mcp/__main__.py",
        "agy_mcp/server.py",
        "agy_mcp/bridge.py",
        "agy_mcp/cli.py",
        "agy_mcp/config.py",
        "agy_mcp/doctor.py",
        "agy_mcp/install.py",
        "agy_mcp/models.py",
        "agy_mcp/routing.py",
        "agy_mcp/safety.py",
        "agy_mcp/session_store.py",
        "agy_mcp/supervisor.py",
        "agy_mcp/utils.py",
        "agy_mcp/worktree.py",
        "agy_mcp/adapters/__init__.py",
        "agy_mcp/adapters/agy.py",
        "agy_mcp/adapters/base.py",
        "agy_mcp/adapters/gemini.py",
        "agy_mcp/adapters/protocol.py",
        "agy_mcp/py.typed",
        "agy_mcp/_skill_bodies/claude/SKILL.md",
        "agy_mcp/_skill_bodies/claude/scripts/agy_bridge.py",
        "agy_mcp/_skill_bodies/claude/references/usage.md",
        "agy_mcp/_skill_bodies/claude/references/prompt-patterns.md",
        "agy_mcp/_skill_bodies/claude/references/security.md",
        "agy_mcp/_skill_bodies/codex/SKILL.md",
        "agy_mcp/_skill_bodies/codex/scripts/agy_bridge.py",
        "agy_mcp/_skill_bodies/codex/references/usage.md",
        "agy_mcp/_skill_bodies/codex/references/prompt-patterns.md",
        "agy_mcp/_skill_bodies/codex/references/security.md",
        "agy_mcp/_skill_bodies/antigravity/SKILL.md",
        "agy_mcp/_skill_bodies/antigravity/references/collaboration.md",
    )
    public_files = (
        "LICENSE",
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
    )
    missing_path = f"agy_mcp/_skill_bodies/{missing_skill}"
    wheel_files = {path: b"# fixture\n" for path in package_members if path != missing_path}
    sdist_files = {f"src/{path}": data for path, data in wheel_files.items()}
    sdist_files.update({path: b"# fixture\n" for path in public_files})
    sdist_files["pyproject.toml"] = (
        b'[project]\nname = "agy-mcp"\nversion = "0.1.8"\n'
        b'[build-system]\nrequires = ["hatchling>=1.21"]\nbuild-backend = "hatchling.build"\n'
    )
    metadata = b"Metadata-Version: 2.4\nName: agy-mcp\nVersion: 0.1.8\n"
    sdist_files["PKG-INFO"] = metadata
    for path, data in sdist_files.items():
        source = project_root / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)

    dist_dir = project_root / "dist"
    dist_dir.mkdir()
    with tarfile.open(dist_dir / "agy_mcp-0.1.8.tar.gz", "w:gz") as archive:
        for path, data in sdist_files.items():
            info = tarfile.TarInfo(f"agy_mcp-0.1.8/{path}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    wheel_files["agy_mcp-0.1.8.dist-info/METADATA"] = metadata
    wheel_files["agy_mcp-0.1.8.dist-info/WHEEL"] = (
        b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    rows = []
    for path, data in wheel_files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.append(f"{path},sha256={digest},{len(data)}\n")
    rows.append("agy_mcp-0.1.8.dist-info/RECORD,,\n")
    wheel_files["agy_mcp-0.1.8.dist-info/RECORD"] = "".join(rows).encode()
    with zipfile.ZipFile(dist_dir / "agy_mcp-0.1.8-py3-none-any.whl", "w") as archive:
        for path, data in wheel_files.items():
            archive.writestr(path, data)

    result = subprocess.run(
        [sys.executable, "-B", "-I", "-S", str(script)],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == expected_code
    assert result.stderr == expected_stderr
