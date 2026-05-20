"""Tests for agy_mcp.session_store — crud, event log, retention."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agy_mcp.models import CanonicalEvent
from agy_mcp.session_store import JobPaths, SessionStore, generate_job_id


def test_generate_job_id_is_unique():
    ids = {generate_job_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(jid.startswith("job_") for jid in ids)


def test_create_and_get_job_round_trip(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    record = store.create_job(cwd="/tmp/repo", request={"prompt": "x"})
    assert record.job_id.startswith("job_")
    fetched = store.get_job(record.job_id)
    assert fetched is not None
    assert fetched.job_id == record.job_id
    assert fetched.status == "running"
    assert fetched.cwd == "/tmp/repo"
    assert fetched.request == {"prompt": "x"}


def test_finalize_job_writes_status_and_exit_code(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    record = store.create_job()
    finalised = store.finalize_job(
        record.job_id, status="completed", exit_code=0, session_id="conv-1"
    )
    assert finalised is not None
    assert finalised.status == "completed"
    assert finalised.exit_code == 0
    assert finalised.session_id == "conv-1"
    assert finalised.finished_at is not None


def test_finalize_job_missing_returns_none(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    assert store.finalize_job("nope", status="failed") is None


def test_append_and_read_events(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    record = store.create_job()
    for i in range(3):
        store.append_event(
            record.job_id,
            CanonicalEvent(type="assistant", text=f"chunk-{i}"),
        )
    events = store.read_events(record.job_id)
    assert len(events) == 3
    assert [e.text for e in events] == ["chunk-0", "chunk-1", "chunk-2"]


def test_read_events_with_since_offset(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    record = store.create_job()
    for i in range(5):
        store.append_event(
            record.job_id,
            CanonicalEvent(type="assistant", text=f"chunk-{i}"),
        )
    events = store.read_events(record.job_id, since=3)
    assert [e.text for e in events] == ["chunk-3", "chunk-4"]


def test_read_events_tolerates_corrupt_line(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    record = store.create_job()
    paths = JobPaths.for_job(tmp_session_root, record.job_id)
    paths.events.write_text(
        json.dumps({"type": "assistant", "text": "good"})
        + "\n{not json at all\n"
        + json.dumps({"type": "assistant", "text": "good-2"})
        + "\n",
        encoding="utf-8",
    )
    events = store.read_events(record.job_id)
    assert len(events) == 3
    # Corrupt line surfaces as an error event in the middle.
    assert events[1].type == "error"
    assert events[1].subtype == "event_decode_failure"
    assert events[0].text == "good"
    assert events[2].text == "good-2"


def test_list_jobs_returns_newest_first(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    job_a = store.create_job()
    time.sleep(0.05)
    job_b = store.create_job()
    time.sleep(0.05)
    job_c = store.create_job()
    listing = store.list_jobs(limit=5)
    assert [r.job_id for r in listing[:3]] == [job_c.job_id, job_b.job_id, job_a.job_id]


def test_list_jobs_limit(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    for _ in range(4):
        store.create_job()
        time.sleep(0.02)
    listing = store.list_jobs(limit=2)
    assert len(listing) == 2


def test_purge_older_than_removes_aged_jobs(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    young = store.create_job()
    old = store.create_job()
    # Backdate the old job's directory mtime
    paths = JobPaths.for_job(tmp_session_root, old.job_id)
    ancient = time.time() - 90 * 86400
    import os

    os.utime(paths.root, (ancient, ancient))
    removed = store.purge_older_than(30)
    assert old.job_id in removed
    assert young.job_id not in removed
    assert store.get_job(young.job_id) is not None
    assert store.get_job(old.job_id) is None


def test_find_by_session_id_returns_most_recent(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    older = store.create_job(session_id="conv-x")
    time.sleep(0.05)
    newer = store.create_job(session_id="conv-x")
    found = store.find_by_session_id("conv-x")
    assert found is not None
    assert found.job_id == newer.job_id


def test_get_job_missing_returns_none(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    assert store.get_job("job_doesnotexist") is None


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "..",
        "../sibling",
        "subdir/nested",
        "",
        "no_prefix_just_text",
        "job_" + "x" * 200,        # exceeds 80-char limit after prefix
        "job_with spaces",
        "job_unicode‮",        # bidi override
        "job_../escape",
        "job_\x00null",
    ],
)
def test_job_id_rejects_traversal_and_garbage(tmp_session_root: Path, bad_id: str):
    store = SessionStore(tmp_session_root)
    with pytest.raises(ValueError):
        store.create_job(job_id=bad_id)
    # Read-only lookup paths must NOT raise — they return None.
    assert store.get_job(bad_id) is None
    assert store.read_events(bad_id) == []


def test_create_job_accepts_only_generated_or_well_formed_ids(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    # No id supplied → generated, accepted.
    auto = store.create_job()
    assert auto.job_id.startswith("job_")
    # Explicit well-formed id — accepted.
    manual = store.create_job(job_id="job_test123_abcDEF")
    assert manual.job_id == "job_test123_abcDEF"


def test_meta_file_is_restrictive(tmp_session_root: Path):
    store = SessionStore(tmp_session_root)
    record = store.create_job()
    paths = JobPaths.for_job(tmp_session_root, record.job_id)
    if paths.meta.exists() and not _is_windows():
        mode = paths.meta.stat().st_mode & 0o777
        assert mode == 0o600


def test_create_job_refuses_duplicate_id(tmp_session_root: Path):
    """Phase 4 R1 P1.2 (sec): explicit duplicate id must not silently
    overwrite the existing meta.json."""

    import pytest as _pytest  # local alias to keep top imports tidy

    store = SessionStore(tmp_session_root)
    first = store.create_job(job_id="job_dup_test")
    assert first.job_id == "job_dup_test"
    with _pytest.raises(FileExistsError):
        store.create_job(job_id="job_dup_test")


def test_append_event_refuses_symlinked_log(tmp_session_root: Path):
    """Phase 4 R1 P2.2 (sec): a planted symlink at events.jsonl must
    not redirect appends to an arbitrary file."""

    if _is_windows():
        return  # pragma: no cover - Windows lacks O_NOFOLLOW semantics
    store = SessionStore(tmp_session_root)
    record = store.create_job()
    paths = JobPaths.for_job(tmp_session_root, record.job_id)
    secret = tmp_session_root / "secret.txt"
    secret.write_text("DO_NOT_OVERWRITE", encoding="utf-8")
    # Replace the auto-touched events.jsonl with a symlink at the same path.
    paths.events.unlink()
    paths.events.symlink_to(secret)
    from agy_mcp.models import CanonicalEvent

    import pytest as _pytest

    with _pytest.raises(OSError):
        store.append_event(record.job_id, CanonicalEvent(type="assistant", text="x"))
    assert secret.read_text(encoding="utf-8") == "DO_NOT_OVERWRITE"


def _is_windows() -> bool:
    import os

    return os.name == "nt"
