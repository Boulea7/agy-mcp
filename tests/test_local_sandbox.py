"""Tests for the built-in local sandbox provider."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agy_mcp import local_sandbox


def _run_json(capsys: pytest.CaptureFixture[str], *args: str) -> dict:
    code = local_sandbox.main([*args, "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert captured.out
    return json.loads(captured.out)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_local_playwright_provider_start_status_attach_logs_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "playwright",
        """#!/usr/bin/env python3
import socket
import sys

args = sys.argv[1:]
if "run-server" not in args:
    raise SystemExit(2)
host = args[args.index("--host") + 1]
port = int(args[args.index("--port") + 1])
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((host, port))
server.listen()
print("fake playwright listening", flush=True)
while True:
    conn, _ = server.accept()
    conn.close()
""",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("AGY_LOCAL_SANDBOX_STATE_DIR", str(tmp_path / "state"))

    start = _run_json(
        capsys,
        "start",
        "--target",
        "pc",
        "--cwd",
        str(tmp_path),
        "--startup-timeout",
        "10",
    )
    sandbox_id = start["sandbox_id"]
    try:
        assert start["success"] is True
        assert start["provider"] == "local"
        assert start["status"] == "running"
        assert start["endpoint"].startswith("ws://127.0.0.1:")
        assert start["metadata"]["playwright_ws_endpoint"] == start["endpoint"]

        status = _run_json(capsys, "status", "--id", sandbox_id)
        assert status["status"] == "running"

        attach = _run_json(capsys, "attach", "--id", sandbox_id)
        assert attach["endpoint"] == start["endpoint"]

        logs = _run_json(capsys, "logs", "--id", sandbox_id, "--tail", "20")
        assert "fake playwright listening" in logs["logs"]
    finally:
        stop = _run_json(capsys, "stop", "--id", sandbox_id)
        assert stop["status"] == "stopped"


def test_local_android_provider_attaches_existing_adb_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "adb",
        """#!/usr/bin/env python3
import sys

args = sys.argv[1:]
if args == ["devices"]:
    print("List of devices attached")
    print("emulator-5554\\tdevice")
elif "getprop" in args and "sys.boot_completed" in args:
    print("1")
elif "emu" in args and "kill" in args:
    raise SystemExit(9)
else:
    print("")
""",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("AGY_LOCAL_SANDBOX_STATE_DIR", str(tmp_path / "state"))

    start = _run_json(
        capsys,
        "start",
        "--target",
        "android",
        "--cwd",
        str(tmp_path),
        "--no-appium",
    )
    assert start["status"] == "running"
    assert start["endpoint"] == "adb:emulator-5554"
    assert start["metadata"]["adb_serial"] == "emulator-5554"
    assert start["metadata"]["owns_emulator"] is False
    assert start["metadata"]["appium_status"] == "disabled"

    stop = _run_json(capsys, "stop", "--id", start["sandbox_id"])
    assert stop["status"] == "stopped"


def test_terminate_process_refuses_start_token_mismatch(
    monkeypatch: pytest.MonkeyPatch,
):
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(local_sandbox, "_pid_running", lambda pid: True)
    monkeypatch.setattr(local_sandbox, "_process_start_token", lambda pid: "proc:new")
    monkeypatch.setattr(local_sandbox.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))

    stopped = local_sandbox._terminate_process(
        {"pid": 123, "pgid": 123, "start_token": "proc:old"},
    )

    assert stopped is False
    assert signals == []


def test_wait_for_android_device_ignores_before_devices(
    monkeypatch: pytest.MonkeyPatch,
):
    ticks = iter([0.0, 0.1, 0.2, 1.1])
    monkeypatch.setattr(local_sandbox.time, "monotonic", lambda: next(ticks, 2.0))
    monkeypatch.setattr(local_sandbox.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(local_sandbox, "_pid_running", lambda pid: True)
    monkeypatch.setattr(local_sandbox, "_online_android_devices", lambda adb: ["emulator-5554"])

    with pytest.raises(local_sandbox.LocalSandboxError, match="timed out"):
        local_sandbox._wait_for_android_device(
            "adb",
            before={"emulator-5554"},
            timeout=1,
            pid=123,
        )


def test_start_rejects_non_loopback_host(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    code = local_sandbox.main(
        [
            "start",
            "--target",
            "pc",
            "--cwd",
            str(tmp_path),
            "--host",
            "0.0.0.0",
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert "loopback" in captured.err


def test_state_path_rejects_dot_segments():
    with pytest.raises(local_sandbox.LocalSandboxError, match="must not"):
        local_sandbox._state_path("..")


def test_child_environment_scrubs_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 32)
    monkeypatch.setenv("ANDROID_HOME", "/android")

    env = local_sandbox._child_environment()

    assert env["PATH"] == "/bin"
    assert env["ANDROID_HOME"] == "/android"
    assert "GITHUB_TOKEN" not in env


def test_tail_log_files_returns_only_recent_lines(tmp_path: Path):
    log = tmp_path / "provider.log"
    log.write_text("\n".join(f"line-{idx}" for idx in range(20)), encoding="utf-8")

    out = local_sandbox._tail_log_files({"logs": {"provider": str(log)}}, tail=3)

    assert "line-16" not in out
    assert "line-17" in out
    assert "line-19" in out


def test_start_android_cleans_owned_emulator_on_boot_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class Args:
        android_avd = "Pixel"
        appium_port = 0
        headless = False
        host = "127.0.0.1"
        no_appium = True
        scenario = ""
        startup_timeout = 1

    cleanup: list[str] = []
    monkeypatch.setattr(
        local_sandbox.shutil,
        "which",
        lambda name: {"adb": "/adb", "emulator": "/emulator"}.get(name),
    )
    monkeypatch.setattr(local_sandbox, "_online_android_devices", lambda _adb: [])
    monkeypatch.setattr(local_sandbox, "_spawn_detached", lambda *_args, **_kwargs: 123)
    monkeypatch.setattr(
        local_sandbox,
        "_process_record",
        lambda **_kwargs: {"pid": 123, "pgid": 123, "start_token": "tok"},
    )
    monkeypatch.setattr(
        local_sandbox,
        "_wait_for_android_device",
        lambda *_args, **_kwargs: "emulator-5554",
    )

    def fail_boot(*_args, **_kwargs):
        raise local_sandbox.LocalSandboxError("boot failed")

    monkeypatch.setattr(local_sandbox, "_wait_for_android_boot", fail_boot)
    monkeypatch.setattr(local_sandbox, "_run_quiet", lambda *_args, **_kwargs: cleanup.append("emu-kill"))
    monkeypatch.setattr(
        local_sandbox,
        "_terminate_process",
        lambda *_args, **_kwargs: cleanup.append("terminate") or True,
    )

    with pytest.raises(local_sandbox.LocalSandboxError, match="boot failed"):
        local_sandbox._start_android(
            Args(),
            sandbox_id="local-mobile-test",
            sandbox_dir=tmp_path,
            cwd=tmp_path,
        )

    assert cleanup == ["emu-kill", "terminate"]


def test_process_record_marks_windows_unverified_when_token_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(local_sandbox.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_sandbox, "_process_group_id", lambda _pid: None)
    monkeypatch.setattr(local_sandbox, "_process_start_token", lambda _pid: "")

    record = local_sandbox._process_record(
        pid=123,
        command=["tool"],
        log_path=tmp_path / "tool.log",
    )

    assert record["identity_unverified"] is True
