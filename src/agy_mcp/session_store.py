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

import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from agy_mcp.models import CanonicalEvent, JobRecord, JobStatus
from agy_mcp.utils import ensure_directory, safe_write_text, utc_now_iso

JOB_ID_PREFIX = "job_"


def generate_job_id() -> str:
    """Return a sortable, URL-safe job id (timestamp + 6 random hex)."""

    return f"{JOB_ID_PREFIX}{int(time.time())}_{secrets.token_hex(3)}"


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
        root = store_root / job_id
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

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        ensure_directory(self.root, mode=0o700)

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
        backend: str | None = None,
    ) -> JobRecord:
        job_id = job_id or generate_job_id()
        paths = JobPaths.for_job(self.root, job_id)
        ensure_directory(paths.root, mode=0o700)
        ensure_directory(paths.artifacts, mode=0o700)
        record = JobRecord(
            job_id=job_id,
            session_id=session_id,
            status="running",
            backend=backend,  # type: ignore[arg-type]
            cwd=cwd,
            log_path=str(paths.agy_log),
            stdout_path=str(paths.stdout),
            stderr_path=str(paths.stderr),
            events_path=str(paths.events),
            request=request or {},
        )
        self._write_meta(paths.meta, record)
        # Touch event log so subsequent appends never need to mkdir again.
        paths.events.touch(exist_ok=True)
        return record

    def get_job(self, job_id: str) -> JobRecord | None:
        paths = JobPaths.for_job(self.root, job_id)
        if not paths.meta.is_file():
            return None
        try:
            data = json.loads(paths.meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return JobRecord.model_validate(data)

    def update_job(self, record: JobRecord) -> JobRecord:
        record.touch()
        paths = JobPaths.for_job(self.root, record.job_id)
        ensure_directory(paths.root, mode=0o700)
        self._write_meta(paths.meta, record)
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
        line = event.model_dump_json(exclude_none=False)
        # Open in append + close-on-each-call mode so concurrent readers see a
        # consistent file even if the writer is killed mid-write. We accept
        # the syscall overhead in exchange for crash safety on long jobs.
        with paths.events.open("a", encoding="utf-8") as fp:
            fp.write(line)
            fp.write("\n")
            fp.flush()
            try:
                os.fsync(fp.fileno())
            except OSError:
                pass

    def read_events(self, job_id: str, *, since: int = 0) -> list[CanonicalEvent]:
        """Read events from offset ``since`` (0-based line index)."""

        paths = JobPaths.for_job(self.root, job_id)
        if not paths.events.is_file():
            return []
        events: list[CanonicalEvent] = []
        with paths.events.open("r", encoding="utf-8") as fp:
            for idx, line in enumerate(fp):
                if idx < since:
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    events.append(CanonicalEvent.model_validate_json(stripped))
                except Exception:
                    # Tolerate corrupt lines (e.g. partial write before crash);
                    # surface as a synthetic error event so the caller still
                    # sees something at this offset.
                    events.append(
                        CanonicalEvent(
                            type="error",
                            subtype="event_decode_failure",
                            text=stripped[:500],
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
        """Delete job directories whose ``updated_at`` is older than ``days``."""

        if days <= 0 or not self.root.is_dir():
            return []
        cutoff = time.time() - days * 86_400
        removed: list[str] = []
        for path in self.root.iterdir():
            if not path.is_dir():
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
