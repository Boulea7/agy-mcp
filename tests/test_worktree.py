"""Tests for agy_mcp.worktree — session-id validation, git worktree lifecycle.

The lifecycle tests need a real ``git`` binary plus a real on-disk repo.
We skip them gracefully when git is missing (CI without git would be
unusual but worth not exploding for).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agy_mcp.worktree import (
    WorktreeContext,
    WorktreeError,
    WorktreeHandle,
    _validate_session_name,
    _worktree_path_for,
    cleanup_worktree,
    create_worktree,
    is_git_repo,
    repo_root,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


GIT_AVAILABLE = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not GIT_AVAILABLE, reason="git binary not on PATH")


def _git(*argv: str, cwd: Path) -> str:
    """Run a git command for test setup; fails loudly if anything goes wrong."""

    proc = subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(argv)} failed in {cwd}: {proc.stderr.strip()}"
        )
    return proc.stdout


@pytest.fixture
def fresh_repo(tmp_path: Path) -> Path:
    """Initialise a brand-new git repo with one commit so HEAD is meaningful."""

    if not GIT_AVAILABLE:
        pytest.skip("git binary not on PATH")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "--initial-branch=main", cwd=repo)
    # Set local user.* so the commit succeeds even on machines without global
    # git identity (CI containers).
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    return repo


# ---------------------------------------------------------------------------
# _validate_session_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["a", "abc", "abc123", "a-b_c.d", "A" * 80],
)
def test_validate_session_name_accepts_well_formed(name: str):
    assert _validate_session_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",                  # empty
        "-leading-dash",     # starts with dash
        ".leading-dot",      # starts with dot
        "_leading-under",    # starts with underscore
        "has/slash",         # path separator
        "has\\backslash",
        "has space",
        "../escape",         # path traversal (contains /)
        "x" * 81,            # over length cap
        "name?question",     # special char
        "name;semicolon",
        "name|pipe",
        "name`backtick`",
        "name$dollar",
    ],
)
def test_validate_session_name_rejects_malformed(name: str):
    with pytest.raises(WorktreeError, match="invalid worktree session id"):
        _validate_session_name(name)


# ---------------------------------------------------------------------------
# is_git_repo / repo_root
# ---------------------------------------------------------------------------


@needs_git
def test_is_git_repo_true_inside_repo(fresh_repo: Path):
    assert is_git_repo(fresh_repo) is True
    sub = fresh_repo / "nested"
    sub.mkdir()
    assert is_git_repo(sub) is True


def test_is_git_repo_false_outside_repo(tmp_path: Path):
    # tmp_path is created by pytest's tmp_path fixture; it is NOT a git repo,
    # and it is far from the test runner cwd that lives in agy-mcp itself.
    assert is_git_repo(tmp_path) is False


def test_is_git_repo_false_on_missing_path(tmp_path: Path):
    assert is_git_repo(tmp_path / "does-not-exist") is False


@needs_git
def test_repo_root_returns_absolute_path(fresh_repo: Path):
    sub = fresh_repo / "a" / "b"
    sub.mkdir(parents=True)
    root = repo_root(sub)
    assert root is not None
    assert root.resolve() == fresh_repo.resolve()


def test_repo_root_returns_none_outside(tmp_path: Path):
    assert repo_root(tmp_path) is None


# ---------------------------------------------------------------------------
# _worktree_path_for
# ---------------------------------------------------------------------------


def test_worktree_path_for_lives_under_dot_agy_mcp(tmp_path: Path):
    path = _worktree_path_for(tmp_path, "session1")
    assert path == (tmp_path / ".agy-mcp" / "worktrees" / "session1").resolve()


def test_worktree_path_for_validates_session_id(tmp_path: Path):
    with pytest.raises(WorktreeError):
        _worktree_path_for(tmp_path, "../escape")


# ---------------------------------------------------------------------------
# create_worktree / cleanup_worktree round trip
# ---------------------------------------------------------------------------


@needs_git
def test_create_and_cleanup_worktree_roundtrip(fresh_repo: Path):
    handle = create_worktree(fresh_repo, "round-trip")
    assert isinstance(handle, WorktreeHandle)
    assert handle.path.is_dir()
    # The worktree's checkout must contain the README we committed.
    assert (handle.path / "README.md").read_text(encoding="utf-8") == "hello"
    # Branch name follows the agy-mcp/<name> convention.
    assert handle.branch == "agy-mcp/round-trip"
    # The path lives under the configured subdir.
    assert (fresh_repo / ".agy-mcp" / "worktrees" / "round-trip").resolve() == handle.path

    cleanup_worktree(handle)
    assert not handle.path.exists()
    # Branch is gone after cleanup.
    branches = _git("branch", "--list", handle.branch, cwd=fresh_repo).strip()
    assert branches == ""


@needs_git
def test_create_worktree_refuses_non_git_cwd(tmp_path: Path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(WorktreeError, match="not a git repository"):
        create_worktree(not_a_repo, "abc")


@needs_git
def test_create_worktree_refuses_duplicate_session(fresh_repo: Path):
    handle = create_worktree(fresh_repo, "dup")
    try:
        with pytest.raises(WorktreeError, match="already exists"):
            create_worktree(fresh_repo, "dup")
    finally:
        cleanup_worktree(handle)


@needs_git
def test_create_worktree_validates_session_id(fresh_repo: Path):
    with pytest.raises(WorktreeError, match="invalid worktree session id"):
        create_worktree(fresh_repo, "../escape")


@needs_git
def test_cleanup_worktree_idempotent_when_path_missing(fresh_repo: Path):
    handle = create_worktree(fresh_repo, "vanish")
    # Simulate the worktree dir disappearing out from under us.
    shutil.rmtree(handle.path)
    # cleanup_worktree must NOT raise — it's documented as idempotent.
    cleanup_worktree(handle)


@needs_git
def test_cleanup_worktree_force_handles_uncommitted(fresh_repo: Path):
    handle = create_worktree(fresh_repo, "dirty")
    (handle.path / "uncommitted.txt").write_text("dirty bits", encoding="utf-8")
    # Without --force git refuses; the wrapper surfaces that as WorktreeError.
    with pytest.raises(WorktreeError, match="git worktree remove exited"):
        cleanup_worktree(handle, force=False)
    # With force=True the cleanup succeeds.
    cleanup_worktree(handle, force=True)
    assert not handle.path.exists()


@needs_git
def test_cleanup_worktree_keep_branch(fresh_repo: Path):
    handle = create_worktree(fresh_repo, "keep-branch")
    cleanup_worktree(handle, delete_branch=False)
    branches = _git("branch", "--list", handle.branch, cwd=fresh_repo).strip()
    assert handle.branch in branches
    # Cleanup the leftover branch so the test stays hermetic.
    _git("branch", "-D", handle.branch, cwd=fresh_repo)


@needs_git
def test_cleanup_worktree_refuses_outside_repo(fresh_repo: Path, tmp_path: Path):
    """Forged WorktreeHandle pointing outside the repo must not be touched."""

    forged = WorktreeHandle(
        path=tmp_path,            # outside fresh_repo
        branch="agy-mcp/forged",
        base_repo=fresh_repo,
        base_ref="HEAD",
    )
    with pytest.raises(WorktreeError, match="outside repo root"):
        cleanup_worktree(forged)


# ---------------------------------------------------------------------------
# WorktreeContext
# ---------------------------------------------------------------------------


@needs_git
def test_worktree_context_cleans_up_on_success(fresh_repo: Path):
    with WorktreeContext(fresh_repo, "ctx-ok") as handle:
        assert handle.path.is_dir()
    assert not handle.path.exists()


@needs_git
def test_worktree_context_preserves_on_exception(fresh_repo: Path):
    handle_box: list[WorktreeHandle] = []
    with pytest.raises(RuntimeError, match="boom"):
        with WorktreeContext(fresh_repo, "ctx-fail") as handle:
            handle_box.append(handle)
            raise RuntimeError("boom")
    # Worktree must remain on disk for post-mortem.
    assert handle_box[0].path.is_dir()
    # Test cleanup so leftover state doesn't pollute subsequent runs.
    cleanup_worktree(handle_box[0], force=True)


@needs_git
def test_worktree_context_always_cleanup_overrides_exception(fresh_repo: Path):
    handle_box: list[WorktreeHandle] = []
    with pytest.raises(RuntimeError, match="boom"):
        with WorktreeContext(fresh_repo, "ctx-always", always_cleanup=True) as handle:
            handle_box.append(handle)
            raise RuntimeError("boom")
    assert not handle_box[0].path.exists()
