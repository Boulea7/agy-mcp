"""On-disk session store backing the long-task supervisor and the MCP `agy_*` tools.

Layout (one directory per job/session under SessionStore.root):

    <root>/<job_id>/
        meta.json        # JobRecord
        events.jsonl     # one CanonicalEvent per line (append-only)
        stdout.log       # raw agy stdout
        stderr.log       # raw agy stderr
        agy.log          # --log-file (klog) destination
        artifacts/       # any files extracted from a turn

The store uses simple file-based locking via atomic rename so that two
supervisor processes do not corrupt ``meta.json`` while still being usable on
filesystems that lack flock semantics (NFS, certain CI containers).
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from pydantic import ValidationError

from agy_mcp.models import BackendName, CanonicalEvent, JobRecord, JobStatus
from agy_mcp.utils import ensure_directory, redact_text, safe_write_text, utc_now_iso

JOB_ID_PREFIX = "job_"
# Strict job-id grammar: callers may supply ids over MCP, so we refuse
# anything that could traverse out of the store root. Generated ids satisfy
# this regex (see generate_job_id).
_JOB_ID_RE = re.compile(r"^job_[A-Za-z0-9_-]{1,80}$")


def generate_job_id() -> str:
    """Return a sortable, URL-safe job id (timestamp + 12 random hex).

    Entropy is 48 bits of randomness on top of a 1s-resolution timestamp.
    Birthday-paradox collision probability at 10 simultaneous bridge
    processes opening 1 job per second sits well below 1e-9; the previous
    24-bit suffix (token_hex(3)) hit ~50% at ~4800 jobs/sec, which the
    Phase 4 single-process supervisor never approaches but a future Phase
    4+ multi-process layout might. (Phase 4 R1 P2#12.)
    """

    return f"{JOB_ID_PREFIX}{int(time.time())}_{secrets.token_hex(6)}"


def _validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        raise ValueError(
            f"invalid job_id {job_id!r}: must match {_JOB_ID_RE.pattern}"
        )
    return job_id


@dataclass(slots=True)
class JobPaths:
    root: Path
    meta: Path
    events: Path
    stdout: Path
    stderr: Path
    agy_log: Path
    artifacts: Path

    @classmethod
    def for_job(cls, store_root: Path, job_id: str) -> "JobPaths":
        _validate_job_id(job_id)
        root = (store_root / job_id).resolve()
        store_resolved = store_root.resolve()
        # Defence-in-depth: even with the strict regex, refuse anything that
        # resolves outside the store root (handles symlinked store roots).
        if not _path_is_relative_to(root, store_resolved):
            raise ValueError(f"job_id {job_id!r} escapes store root")
        return cls(
            root=root,
            meta=root / "meta.json",
            events=root / "events.jsonl",
            stdout=root / "stdout.log",
            stderr=root / "stderr.log",
            agy_log=root / "agy.log",
            artifacts=root / "artifacts",
        )


class SessionStore:
    """File-backed session/job store with append-only event log."""

    def __init__(
        self,
        root: Path,
        *,
        clock: "Callable[[], float] | None" = None,
    ) -> None:
        self.root = Path(root)
        ensure_directory(self.root, mode=0o700)
        # ``clock`` is an injection seam for tests so they can pin the
        # mtime of job directories without sleeping between create_job
        # calls. ``None`` keeps the production path on the OS-default
        # mtime; when provided, ``create_job`` / ``update_job`` rewrite
        # the dir mtime with ``os.utime`` so ordering is deterministic.
        self._clock = clock

    # ------------------------------------------------------------------
    # Job lifecycle
    # ------------------------------------------------------------------

    def create_job(
        self,
        *,
        job_id: str | None = None,
        session_id: str | None = None,
        cwd: str = "",
        request: dict | None = None,
        backend: BackendName | None = None,
        pid: int | None = None,
        extra: dict | None = None,
    ) -> JobRecord:
        job_id = _validate_job_id(job_id) if job_id is not None else generate_job_id()
        paths = JobPaths.for_job(self.root, job_id)
        # Phase 4 R1 P1.2: refuse to overwrite an existing job. Without
        # this check a caller supplying an explicit job_id could silently
        # replace another job's meta.json (and worse: overwrite the
        # in-memory _JobHandle inside Supervisor._jobs, orphaning the
        # original worker's cancel_event). Auto-generated ids encode a
        # second-resolution timestamp + 48 bits of entropy, so collisions
        # there indicate either a clock glitch or a duplicate retry —
        # both deserve a hard error rather than silent overwrite.
        if paths.meta.exists():
            raise FileExistsError(
                f"job_id {job_id!r} already exists at {paths.meta}",
            )
        ensure_directory(paths.root, mode=0o700)
        ensure_directory(paths.artifacts, mode=0o700)
        record = JobRecord(
            job_id=job_id,
            session_id=session_id,
            status="running",
            backend=backend,
            cwd=cwd,
            pid=pid,
            log_path=str(paths.agy_log),
            stdout_path=str(paths.stdout),
            stderr_path=str(paths.stderr),
            events_path=str(paths.events),
            request=request or {},
            extra=extra or {},
        )
        self._write_meta(paths.meta, record)
        # Touch event log so subsequent appends never need to mkdir again.
        paths.events.touch(exist_ok=True)
        self._stamp(paths.root)
        return record

    def get_job(self, job_id: str) -> JobRecord | None:
        try:
            paths = JobPaths.for_job(self.root, job_id)
        except ValueError:
            return None
        if not paths.meta.is_file():
            return None
        try:
            data = json.loads(paths.meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return JobRecord.model_validate(data)
        except ValidationError:
            return None

    def update_job(self, record: JobRecord) -> JobRecord:
        record.touch()
        paths = JobPaths.for_job(self.root, record.job_id)
        ensure_directory(paths.root, mode=0o700)
        self._write_meta(paths.meta, record)
        self._stamp(paths.root)
        return record

    def finalize_job(
        self,
        job_id: str,
        *,
        status: JobStatus,
        exit_code: int | None = None,
        session_id: str | None = None,
        error: str | None = None,
    ) -> JobRecord | None:
        record = self.get_job(job_id)
        if record is None:
            return None
        record.status = status
        record.exit_code = exit_code
        record.finished_at = utc_now_iso()
        if session_id and not record.session_id:
            record.session_id = session_id
        if error is not None:
            record.error = error
        return self.update_job(record)

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    def append_event(self, job_id: str, event: CanonicalEvent) -> None:
        paths = JobPaths.for_job(self.root, job_id)
        ensure_directory(paths.root, mode=0o700)
        # Single concatenated write — line is written atomically up to
        # PIPE_BUF on POSIX; on networked filesystems writers must be serial
        # per job (supervisor guarantees one writer per job_id).
        line = event.model_dump_json(exclude_none=False) + "\n"
        fp = _open_append_no_follow(paths.events)
        try:
            fp.write(line)
            fp.flush()
            try:
                os.fsync(fp.fileno())
            except OSError:
                pass
        finally:
            try:
                fp.close()
            except OSError:
                pass

    def read_events(self, job_id: str, *, since: int = 0) -> list[CanonicalEvent]:
        """Read events from offset ``since`` (0-based line index)."""

        try:
            paths = JobPaths.for_job(self.root, job_id)
        except ValueError:
            return []
        try:
            st = paths.events.stat(follow_symlinks=False)
        except FileNotFoundError:
            return []
        except OSError as exc:
            return [_event_log_unreadable(exc)]
        if not stat.S_ISREG(st.st_mode):
            return [
                CanonicalEvent(
                    type="error",
                    subtype="event_log_unreadable",
                    text="event log is not a regular file",
                )
            ]
        events: list[CanonicalEvent] = []
        try:
            fp = _open_read_no_follow(paths.events)
        except OSError as exc:
            return [_event_log_unreadable(exc)]
        with fp:
            for idx, line in enumerate(fp):
                if idx < since:
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    events.append(CanonicalEvent.model_validate_json(stripped))
                except (json.JSONDecodeError, ValidationError):
                    # Tolerate corrupt lines (partial write before crash);
                    # surface as a synthetic error event so the caller still
                    # sees something at this offset.
                    events.append(
                        CanonicalEvent(
                            type="error",
                            subtype="event_decode_failure",
                            text=redact_text(stripped)[:500],
                        )
                    )
        return events

    # ------------------------------------------------------------------
    # Listings & retention
    # ------------------------------------------------------------------

    def list_jobs(self, *, limit: int | None = 50) -> list[JobRecord]:
        if not self.root.is_dir():
            return []
        candidates = [p for p in self.root.iterdir() if p.is_dir()]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        records: list[JobRecord] = []
        for path in candidates:
            if limit is not None and len(records) >= limit:
                break
            record = self.get_job(path.name)
            if record is not None:
                records.append(record)
        return records

    def purge_older_than(self, days: int) -> list[str]:
        """Delete job directories whose meta mtime is older than ``days``.

        Uses the filesystem mtime rather than ``JobRecord.updated_at`` so that
        purge cost stays O(n) without loading every meta.json. Callers that
        depend on the record timestamp should call ``update_job(record)`` to
        bump the mtime.
        """

        if days <= 0 or not self.root.is_dir():
            return []
        cutoff = time.time() - days * 86_400
        removed: list[str] = []
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            # Reject anything outside the store root after symlink resolution.
            try:
                if not _path_is_relative_to(path.resolve(), self.root.resolve()):
                    continue
            except OSError:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    _rmtree(path)
                    removed.append(path.name)
            except OSError:
                continue
        return removed

    def find_by_session_id(self, session_id: str) -> JobRecord | None:
        """Return the most recent job recorded with the given session_id."""

        for record in self.list_jobs(limit=200):
            if record.session_id == session_id:
                return record
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _write_meta(target: Path, record: JobRecord) -> None:
        safe_write_text(
            target,
            record.model_dump_json(exclude_none=False, indent=2),
            mode=0o600,
        )

    def _stamp(self, path: Path) -> None:
        """Pin the directory mtime when an injected clock is in use.

        Production code path (``clock=None``) keeps the OS-default mtime
        so retention behaviour matches what an operator would expect from
        an ``ls -lt`` of the store. Tests pass a deterministic clock to
        eliminate ``time.sleep(0.05)`` between back-to-back ``create_job``
        calls when verifying ``list_jobs`` ordering. Errors are swallowed
        — failing to set the mtime on a perfectly-good job dir should not
        block the create/update path.
        """

        if self._clock is None:
            return
        try:
            stamp = float(self._clock())
        except (TypeError, ValueError):
            return
        try:
            os.utime(path, (stamp, stamp))
        except OSError:
            return


def _rmtree(path: Path) -> None:
    """Best-effort recursive remove that tolerates concurrent unlink races."""

    for child in path.iterdir() if path.is_dir() else ():
        if child.is_dir() and not child.is_symlink():
            _rmtree(child)
        else:
            try:
                child.unlink()
            except OSError:
                pass
    try:
        path.rmdir()
    except OSError:
        pass


def _path_is_relative_to(child: Path, parent: Path) -> bool:
    """Backport of ``Path.is_relative_to`` for Python 3.8/3.9 compatibility."""

    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _open_append_no_follow(path: Path):
    """Open ``path`` for append with O_NOFOLLOW when the OS supports it.

    Mirrors :func:`agy_mcp.adapters.base._open_spool`. Phase 4 R1 P2.2
    promotes this from the latent followups list to a Phase 4 invariant
    because :class:`StoreEventSink.emit` now writes to ``events.jsonl``
    on every adapter event; an attacker who can plant a symlink under
    a job dir (e.g. via a custom ``AGY_MCP_SESSION_ROOT``) could
    otherwise redirect those appends to an arbitrary file.

    On a filesystem that rejects ``O_NOFOLLOW`` (rare; some NFS/FUSE
    mounts), we fall back to a plain append after one more
    ``is_symlink`` check so we still refuse the symlinked target.
    """

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    nofollow_supported = hasattr(os, "O_NOFOLLOW")
    if nofollow_supported:
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise
        if not nofollow_supported or exc.errno in (
            errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL,
        ):
            if path.is_symlink():
                raise OSError(
                    errno.ELOOP, f"refusing to follow symlink: {path}",
                ) from exc
            return path.open("a", encoding="utf-8")
        raise
    # Phase 4 R2 sec P3.1: if ``os.fdopen`` raises after the raw fd is
    # in hand, close it explicitly so we don't leak. Practically
    # near-zero risk (the wrapper does no I/O) but cheap insurance and
    # matches the pattern in supervisor._safe_copyfile.
    try:
        return os.fdopen(fd, "a", encoding="utf-8")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _open_read_no_follow(path: Path):
    """Open ``path`` for reading without following a leaf symlink."""

    flags = os.O_RDONLY
    nofollow_supported = hasattr(os, "O_NOFOLLOW")
    if nofollow_supported:
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise
        if not nofollow_supported or exc.errno in (
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
            errno.EINVAL,
        ):
            if path.is_symlink():
                raise OSError(
                    errno.ELOOP, f"refusing to follow symlink: {path}",
                ) from exc
            fp = path.open("r", encoding="utf-8")
            try:
                st = os.fstat(fp.fileno())
                if not stat.S_ISREG(st.st_mode):
                    raise OSError(
                        errno.EINVAL, f"event log is not regular: {path}"
                    )
                return fp
            except BaseException:
                fp.close()
                raise
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, f"event log is not regular: {path}")
        return os.fdopen(fd, "r", encoding="utf-8")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _event_log_unreadable(exc: OSError) -> CanonicalEvent:
    return CanonicalEvent(
        type="error",
        subtype="event_log_unreadable",
        text=redact_text(str(exc))[:500],
    )


def collect_artifact_paths(records: Iterable[JobRecord]) -> list[str]:
    """Flatten the artifact paths recorded across multiple jobs."""

    out: list[str] = []
    for record in records:
        for art in record.artifacts:
            path = art.get("path") if isinstance(art, dict) else None
            if path:
                out.append(str(path))
    return out


__all__ = [
    "JobPaths",
    "JOB_ID_PREFIX",
    "SessionStore",
    "collect_artifact_paths",
    "generate_job_id",
]
