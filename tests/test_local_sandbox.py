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
