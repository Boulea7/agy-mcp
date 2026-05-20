"""Git-worktree helper used by execute+allow_write requests.

The worktree gives ``agy --execute`` a sandboxed copy of the user's repo so a
mis-step (rm -rf in the wrong dir, a runaway refactor, an unintended commit)
never reaches the main checkout. Cleanup is best-effort but documented: on
crash, the worktree directory remains and the caller can `git worktree
prune` it later.

Layout::

    <repo_root>/
    └── .agy-mcp/
        └── worktrees/
            └── <session_id>/         <- the worktree path
                ├── .git              <- gitlink
                └── ... checkout ...

Branch naming::

    agy-mcp/<session_id>              <- created at worktree time

Both the worktree path and the branch are derived from a caller-supplied
``session_id`` that is hardened against path-traversal: only letters,
digits, dot, underscore, dash; max 80 chars.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_SESSION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

# Subdir of the repo where worktrees live. Plan says ``.agy-mcp/worktrees``
# (gitignored at repo root); we follow that.
_WORKTREE_DIR_NAME = ".agy-mcp"
_WORKTREE_SUBDIR = "worktrees"

# Subprocess timeout for git invocations. git worktree create/remove are
# fast on a healthy repo; if it takes >30s something is wrong.
_GIT_TIMEOUT_S = 30


# ---------------------------------------------------------------------------
# Handle
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WorktreeHandle:
    """Returned by :func:`create_worktree`; pass to :func:`cleanup_worktree`."""

    path: Path          # absolute path to the worktree checkout
    branch: str         # branch name created by ``git worktree add -b``
    base_repo: Path     # absolute path to the main repo root
    base_ref: str       # ref the worktree was created from (HEAD by default)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class WorktreeError(RuntimeError):
    """Raised on any worktree creation or cleanup failure."""


def is_git_repo(path: Path) -> bool:
    """Return True iff ``path`` (or a sane ancestor) is inside a git work-tree.

    Uses ``git rev-parse --is-inside-work-tree``; that's the canonical check
    git itself relies on, and it handles submodules and worktrees correctly.
    """

    if not path.exists() or not path.is_dir():
        return False
    try:
        proc = subprocess.run(  # noqa: S603 - argv hard-coded
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def repo_root(path: Path) -> Path | None:
    """Return the absolute path to the repo root, or None if ``path`` isn't a repo."""

    try:
        proc = subprocess.run(  # noqa: S603 - argv hard-coded
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return Path(root).resolve() if root else None


def _validate_session_name(session_id: str) -> str:
    if not session_id or not _SESSION_NAME.match(session_id):
        raise WorktreeError(
            f"invalid worktree session id: {session_id!r} — must match "
            r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"
        )
    return session_id


def _worktree_path_for(repo: Path, session_id: str) -> Path:
    name = _validate_session_name(session_id)
    candidate = (repo / _WORKTREE_DIR_NAME / _WORKTREE_SUBDIR / name).resolve()
    # Containment: refuse anything that escapes the repo. ``relative_to``
    # raises ValueError when the path is outside; we convert to WorktreeError.
    try:
        candidate.relative_to(repo)
    except ValueError as exc:
        raise WorktreeError(
            f"worktree path escapes repo root: {candidate!r} not under {repo!r}"
        ) from exc
    return candidate


def create_worktree(
    cwd: Path,
    session_id: str,
    *,
    base_ref: str = "HEAD",
) -> WorktreeHandle:
    """Create a fresh worktree for ``session_id`` and return its handle.

    Raises :class:`WorktreeError` on any failure (not a git repo, name
    rejected, git command failed, worktree already exists, etc.).
    """

    # strict=True refuses dangling symlinks fail-closed (Phase 3 review P1.4).
    # Without it, a broken symlink at ``cwd`` would silently resolve to its
    # nonexistent target and ``git rev-parse`` would run in whatever ``cwd``
    # the parent process happened to be in.
    try:
        cwd = Path(cwd).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorktreeError(f"cwd does not resolve to a real path: {cwd!r}: {exc}") from exc

    root = repo_root(cwd)
    if root is None:
        raise WorktreeError(f"cwd is not a git repository: {cwd}")

    name = _validate_session_name(session_id)
    worktree_path = _worktree_path_for(root, name)
    branch = f"agy-mcp/{name}"

    # Atomic-create worktree_path with 0o700 mode in a single syscall. This
    # collapses the prior ``exists() → mkdir → chmod → git add`` window where
    # a concurrent attacker could plant a symlink between the exists check
    # and the git invocation (Phase 3 review M1). ``os.mkdir`` is the right
    # primitive here because it never follows symlinks for the leaf and it
    # refuses if the leaf already exists.
    parent = worktree_path.parent
    _ensure_parent_dir(parent)

    # Pre-create the worktree leaf so we own it and can hand it to git. Git's
    # ``worktree add`` accepts an existing empty directory; this guarantees
    # the leaf cannot be a symlink because os.mkdir refused to follow one.
    try:
        os.mkdir(worktree_path, mode=0o700)
    except FileExistsError as exc:
        raise WorktreeError(
            f"worktree path already exists: {worktree_path} — choose a different "
            "session id or call cleanup_worktree() first"
        ) from exc
    except OSError as exc:
        raise WorktreeError(f"failed to allocate worktree dir {worktree_path}: {exc}") from exc

    # git refuses to add into a non-empty directory; passing an empty dir
    # works on git >= 2.32. On older git we'd need rmdir-then-add, but the
    # project requires >= 2.40 (see pyproject.toml).
    argv = [
        "git", "worktree", "add",
        "-b", branch,
        str(worktree_path),
        base_ref,
    ]
    try:
        proc = subprocess.run(  # noqa: S603 - argv hard-coded
            argv,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Roll back the empty dir we just allocated so a retry can succeed.
        _safe_rmdir(worktree_path)
        raise WorktreeError(f"git worktree add failed to launch: {exc}") from exc
    if proc.returncode != 0:
        _safe_rmdir(worktree_path)
        # Surface a redact-safe error: git's stderr never contains secrets
        # in this command, but we cap it to keep the response envelope small.
        stderr = (proc.stderr or "").strip()[:500]
        raise WorktreeError(
            f"git worktree add exited {proc.returncode}: {stderr}"
        )

    return WorktreeHandle(
        path=worktree_path,
        branch=branch,
        base_repo=root,
        base_ref=base_ref,
    )


def _ensure_parent_dir(parent: Path) -> None:
    """Create ``parent`` with 0o700 perms, refusing if it's a symlink.

    ``Path.chmod`` follows symlinks (it calls ``os.chmod`` not ``os.lchmod``),
    so the previous "mkdir then chmod" sequence had a TOCTOU window where an
    attacker could swap the parent to a symlink targeting a sensitive dir
    (Phase 3 review M2). We refuse fail-closed if anything in the parent
    chain is already a symlink.
    """

    parent.mkdir(parents=True, exist_ok=True)
    try:
        st = os.lstat(parent)
    except OSError as exc:
        raise WorktreeError(f"failed to stat worktree parent {parent}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise WorktreeError(
            f"refusing to use symlinked worktree parent: {parent}"
        )
    # Use the no-follow chmod variant where supported; otherwise fall back to
    # plain chmod (the lstat guard above already excluded symlinks, so plain
    # chmod is safe here).
    try:
        os.chmod(parent, 0o700, follow_symlinks=False)  # type: ignore[call-arg]
    except (OSError, NotImplementedError) as exc:
        if isinstance(exc, OSError) and exc.errno not in (
            errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL,
        ):
            raise WorktreeError(f"failed to chmod worktree parent: {exc}") from exc
        try:
            os.chmod(parent, 0o700)
        except OSError:
            # Best-effort on filesystems that ignore chmod entirely.
            pass


def _safe_rmdir(path: Path) -> None:
    """Remove an empty directory we just created. Never follows symlinks."""

    try:
        os.rmdir(path)
    except OSError:
        # Either already gone, contains stuff git wrote, or a symlink we
        # refused to traverse. Leave it for human inspection.
        pass


def cleanup_worktree(
    handle: WorktreeHandle,
    *,
    force: bool = False,
    delete_branch: bool = True,
) -> None:
    """Remove a worktree previously created by :func:`create_worktree`.

    ``force=True`` passes ``--force`` to ``git worktree remove`` so a worktree
    with uncommitted changes still gets cleaned up. The created branch is
    deleted afterwards unless ``delete_branch=False``.

    Errors are logged via :class:`WorktreeError` so callers can surface them
    in BridgeResponse / log_path; do NOT swallow them silently. We never
    attempt to remove the main repo or any path outside the configured
    worktree subdir — :func:`create_worktree` enforced that invariant.
    """

    if not handle.path.exists():
        # Idempotent: nothing to remove.
        return

    # Defensive: re-check that the worktree path still lives under the repo.
    try:
        handle.path.resolve().relative_to(handle.base_repo)
    except ValueError as exc:
        raise WorktreeError(
            f"refusing to remove worktree outside repo root: {handle.path}"
        ) from exc

    argv = ["git", "worktree", "remove"]
    if force:
        argv.append("--force")
    argv.append(str(handle.path))

    try:
        proc = subprocess.run(  # noqa: S603 - argv hard-coded
            argv,
            cwd=str(handle.base_repo),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorktreeError(f"git worktree remove failed to launch: {exc}") from exc

    if proc.returncode != 0:
        # ``git worktree remove`` refuses on uncommitted changes unless we
        # pass --force; report cleanly so the caller can decide whether to
        # retry with force=True.
        stderr = (proc.stderr or "").strip()[:500]
        raise WorktreeError(
            f"git worktree remove exited {proc.returncode}: {stderr}"
        )

    if delete_branch:
        _delete_branch(handle)


def _delete_branch(handle: WorktreeHandle) -> bool:
    """Delete the branch created by ``git worktree add -b`` (best-effort).

    Returns True on success, False otherwise (branch gone, git error, spawn
    failure). Callers can log the False result in debug mode without crashing
    the cleanup flow.
    """

    try:
        proc = subprocess.run(  # noqa: S603 - argv hard-coded
            ["git", "branch", "-D", handle.branch],
            cwd=str(handle.base_repo),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Convenience context manager
# ---------------------------------------------------------------------------


class WorktreeContext:
    """``with WorktreeContext(cwd, session_id) as handle: ...`` helper.

    On exit, the worktree is removed unless the block raised — in which case
    we preserve the worktree (force=False) so the user can inspect what went
    wrong. Pass ``always_cleanup=True`` to remove unconditionally.
    """

    def __init__(
        self,
        cwd: Path,
        session_id: str,
        *,
        base_ref: str = "HEAD",
        always_cleanup: bool = False,
    ) -> None:
        self.cwd = cwd
        self.session_id = session_id
        self.base_ref = base_ref
        self.always_cleanup = always_cleanup
        self.handle: WorktreeHandle | None = None
        self._lock = threading.Lock()

    def __enter__(self) -> WorktreeHandle:
        with self._lock:
            self.handle = create_worktree(self.cwd, self.session_id, base_ref=self.base_ref)
            return self.handle

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        with self._lock:
            handle = self.handle
        if handle is None:
            return False
        if exc_type is not None and not self.always_cleanup:
            # Leave the worktree behind for post-mortem; document the path.
            return False
        try:
            cleanup_worktree(handle, force=True)
        except WorktreeError:
            # Cleanup failed; don't shadow the original exception if any.
            return False
        return False


__all__ = [
    "WorktreeContext",
    "WorktreeError",
    "WorktreeHandle",
    "cleanup_worktree",
    "create_worktree",
    "is_git_repo",
    "repo_root",
]
