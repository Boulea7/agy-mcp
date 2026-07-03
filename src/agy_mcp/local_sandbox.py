"""Built-in local sandbox provider for Playwright and Android emulator tests."""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_ANDROID_TARGETS = {"android", "emulator", "mobile"}
_BROWSER_TARGETS = {"browser", "playwright", "web"}
_VM_TARGETS = {"desktop", "desktop-vm", "pc", "pc-vm", "vm"}
_SANDBOX_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_STARTUP_TIMEOUT = 60
_LOG_TAIL_MAX_LINES = 10_000
_LIVE_PROCS: dict[int, subprocess.Popen[str]] = {}
_CHILD_ENV_ALLOWLIST = {
    "APPDATA",
    "ANDROID_HOME",
    "ANDROID_SDK_ROOT",
    "COMSPEC",
    "HOME",
    "JAVA_HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PLAYWRIGHT_BROWSERS_PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
}


class LocalSandboxError(RuntimeError):
    """User-facing provider failure."""


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local sandbox provider CLI."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            result = _cmd_start(args)
        elif args.command == "status":
            result = _cmd_status(args)
        elif args.command == "stop":
            result = _cmd_stop(args)
        elif args.command == "logs":
            result = _cmd_logs(args)
        elif args.command == "attach":
            result = _cmd_attach(args)
        else:  # pragma: no cover - argparse prevents this.
            parser.error("missing command")
    except LocalSandboxError as exc:
        _emit_error(exc, json_output=getattr(args, "json", False))
        return 1

    _emit(result, json_output=args.json)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agy-local-sandbox",
        description=(
            "Start and control local browser or Android emulator sandboxes. "
            "This provider uses only tools already installed on the local machine."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start a local sandbox")
    start.add_argument(
        "--target",
        required=True,
        help="browser/playwright or mobile/android; PC/desktop VM targets require another provider",
    )
    start.add_argument("--scenario", default="", help="optional scenario label")
    start.add_argument("--cwd", default=".", help="project working directory")
    start.add_argument("--host", default=_DEFAULT_HOST, help="loopback host for local servers")
    start.add_argument("--port", type=int, default=0, help="Playwright port; 0 picks a free port")
    start.add_argument("--startup-timeout", type=int, default=_DEFAULT_STARTUP_TIMEOUT)
    start.add_argument("--android-avd", default="", help="Android Virtual Device name to boot")
    start.add_argument("--appium-port", type=int, default=0, help="Appium port; 0 picks a free port")
    start.add_argument("--headless", action="store_true", help="boot Android emulator without a UI")
    start.add_argument("--no-appium", action="store_true", help="skip optional Appium server startup")
    start.add_argument("--json", action="store_true", help="emit JSON")

    for name in ("status", "stop", "attach"):
        command = subparsers.add_parser(name, help=f"{name} a local sandbox")
        command.add_argument("--id", required=True, dest="sandbox_id")
        command.add_argument("--json", action="store_true", help="emit JSON")

    logs = subparsers.add_parser("logs", help="read local sandbox logs")
    logs.add_argument("--id", required=True, dest="sandbox_id")
    logs.add_argument("--tail", type=int, default=200)
    logs.add_argument("--json", action="store_true", help="emit JSON")

    return parser


def _cmd_start(args: argparse.Namespace) -> dict[str, Any]:
    target = _canonical_target(args.target)
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise LocalSandboxError(f"cwd is not a directory: {args.cwd}")
    if args.startup_timeout <= 0:
        raise LocalSandboxError("startup-timeout must be positive seconds")
    if args.port < 0 or args.appium_port < 0:
        raise LocalSandboxError("ports must be non-negative")
    _validate_loopback_host(args.host)

    sandbox_id = _new_sandbox_id(target)
    sandbox_dir = _state_root() / sandbox_id
    sandbox_dir.mkdir(parents=True, exist_ok=False)
    try:
        sandbox_dir.chmod(0o700)
    except OSError:
        pass

    if target == "browser":
        state = _start_playwright(args, sandbox_id=sandbox_id, sandbox_dir=sandbox_dir, cwd=cwd)
    else:
        state = _start_android(args, sandbox_id=sandbox_id, sandbox_dir=sandbox_dir, cwd=cwd)
    _write_state(state)
    return _response(state)


def _start_playwright(
    args: argparse.Namespace,
    *,
    sandbox_id: str,
    sandbox_dir: Path,
    cwd: Path,
) -> dict[str, Any]:
    command = _playwright_command(cwd)
    if command is None:
        raise LocalSandboxError(
            "Playwright CLI not found; install Playwright in the project "
            "(`npm i -D @playwright/test` or equivalent) or set "
            "AGY_LOCAL_SANDBOX_PLAYWRIGHT_CMD."
        )

    port = args.port or _free_tcp_port(args.host)
    log_path = sandbox_dir / "playwright.log"
    launch_command = [*command, "--host", args.host, "--port", str(port)]
    pid = _spawn_detached(launch_command, cwd=cwd, log_path=log_path)
    process = _process_record(pid=pid, command=launch_command, log_path=log_path)
    try:
        _wait_for_tcp(args.host, port, args.startup_timeout, pid=pid)
    except LocalSandboxError:
        _terminate_process(process, allow_unverified=True)
        raise

    endpoint = f"ws://{args.host}:{port}/"
    now = time.time()
    return {
        "sandbox_id": sandbox_id,
        "provider": "local",
        "target": "browser",
        "kind": "playwright",
        "status": "running",
        "endpoint": endpoint,
        "created_at": now,
        "updated_at": now,
        "cwd": str(cwd),
        "scenario": args.scenario,
        "processes": {"playwright": process},
        "logs": {"playwright": str(log_path)},
        "metadata": {
            "playwright_ws_endpoint": endpoint,
            "playwright_command": Path(command[0]).name,
            "host": args.host,
            "port": port,
        },
    }


def _start_android(
    args: argparse.Namespace,
    *,
    sandbox_id: str,
    sandbox_dir: Path,
    cwd: Path,
) -> dict[str, Any]:
    adb = shutil.which("adb")
    if not adb:
        raise LocalSandboxError(
            "adb not found on PATH; install Android Platform Tools before "
            "using the local mobile sandbox provider."
        )

    before = set(_online_android_devices(adb))
    serial = sorted(before)[0] if before else ""
    owns_emulator = False
    emulator_avd = ""
    emulator_process: dict[str, Any] | None = None
    logs: dict[str, str] = {}

    try:
        if not serial:
            emulator = shutil.which("emulator")
            if not emulator:
                raise LocalSandboxError(
                    "no online Android device and emulator CLI not found on PATH; "
                    "install Android Emulator or connect a device visible to adb."
                )
            emulator_avd = (
                args.android_avd
                or os.environ.get("AGY_LOCAL_SANDBOX_ANDROID_AVD", "")
                or _first_android_avd(emulator)
            )
            if not emulator_avd:
                raise LocalSandboxError(
                    "no Android AVD found; create one with Android Studio or pass --android-avd."
                )

            emulator_log = sandbox_dir / "emulator.log"
            command = [emulator, "-avd", emulator_avd, "-no-snapshot-save"]
            if args.headless or _env_true("AGY_LOCAL_SANDBOX_ANDROID_HEADLESS"):
                command.extend(["-no-window", "-no-audio"])
            emulator_pid = _spawn_detached(command, cwd=cwd, log_path=emulator_log)
            emulator_process = _process_record(pid=emulator_pid, command=command, log_path=emulator_log)
            owns_emulator = True
            logs["emulator"] = str(emulator_log)
            serial = _wait_for_android_device(
                adb,
                before=before,
                timeout=args.startup_timeout,
                pid=emulator_pid,
            )

        _wait_for_android_boot(adb, serial=serial, timeout=args.startup_timeout)
    except Exception:
        if owns_emulator:
            if serial:
                _run_quiet([adb, "-s", serial, "emu", "kill"], timeout=5)
            _terminate_process(emulator_process, allow_unverified=True)
        raise
    appium = _start_appium(args, sandbox_dir=sandbox_dir, cwd=cwd)
    logs.update(appium.pop("logs", {}))
    appium_process = appium.pop("process", None)
    endpoint = appium.get("appium_url") or f"adb:{serial}"
    now = time.time()
    processes: dict[str, dict[str, Any]] = {}
    if emulator_process is not None:
        processes["emulator"] = emulator_process
    if isinstance(appium_process, dict):
        processes["appium"] = appium_process

    return {
        "sandbox_id": sandbox_id,
        "provider": "local",
        "target": "mobile",
        "kind": "android",
        "status": "running",
        "endpoint": endpoint,
        "created_at": now,
        "updated_at": now,
        "cwd": str(cwd),
        "scenario": args.scenario,
        "processes": processes,
        "logs": logs,
        "metadata": {
            "adb_serial": serial,
            "adb_endpoint": f"adb:{serial}",
            "owns_emulator": owns_emulator,
            "emulator_avd": emulator_avd,
            **appium,
        },
    }


def _start_appium(
    args: argparse.Namespace,
    *,
    sandbox_dir: Path,
    cwd: Path,
) -> dict[str, Any]:
    if args.no_appium:
        return {"appium_status": "disabled"}
    appium = shutil.which("appium")
    if not appium:
        return {"appium_status": "not_found"}

    port = args.appium_port or _free_tcp_port(args.host)
    log_path = sandbox_dir / "appium.log"
    command = [appium, "--address", args.host, "--port", str(port)]
    pid = _spawn_detached(command, cwd=cwd, log_path=log_path)
    process = _process_record(pid=pid, command=command, log_path=log_path)
    try:
        _wait_for_tcp(args.host, port, min(args.startup_timeout, 20), pid=pid)
    except LocalSandboxError:
        _terminate_process(process, allow_unverified=True)
        return {
            "appium_status": "failed",
            "appium_error": "Appium process did not open its TCP port before timeout.",
            "logs": {"appium": str(log_path)},
        }
    return {
        "appium_status": "running",
        "appium_pid": pid,
        "appium_url": f"http://{args.host}:{port}/",
        "appium_host": args.host,
        "appium_port": port,
        "logs": {"appium": str(log_path)},
        "process": process,
    }


def _cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    state = _load_state(args.sandbox_id)
    _refresh_state(state)
    _write_state(state)
    return _response(state)


def _cmd_stop(args: argparse.Namespace) -> dict[str, Any]:
    state = _load_state(args.sandbox_id)
    kind = state.get("kind")
    processes = state.get("processes", {}) if isinstance(state.get("processes"), dict) else {}

    if kind == "android":
        _terminate_process(_process_info(processes, "appium"))
        metadata = state.get("metadata", {}) if isinstance(state.get("metadata"), dict) else {}
        if metadata.get("owns_emulator") and metadata.get("adb_serial"):
            adb = shutil.which("adb")
            if adb:
                _run_quiet([adb, "-s", str(metadata["adb_serial"]), "emu", "kill"], timeout=5)
        _terminate_process(_process_info(processes, "emulator"))
    else:
        _terminate_process(_process_info(processes, "playwright"))

    state["status"] = "stopped"
    state["updated_at"] = time.time()
    _write_state(state)
    return _response(state)


def _cmd_logs(args: argparse.Namespace) -> dict[str, Any]:
    if args.tail <= 0:
        raise LocalSandboxError("tail must be positive")
    if args.tail > _LOG_TAIL_MAX_LINES:
        raise LocalSandboxError(f"tail exceeds {_LOG_TAIL_MAX_LINES} lines")
    state = _load_state(args.sandbox_id)
    result = _response(state)
    result["logs"] = _tail_log_files(state, tail=args.tail)
    return result


def _cmd_attach(args: argparse.Namespace) -> dict[str, Any]:
    state = _load_state(args.sandbox_id)
    _refresh_state(state)
    _write_state(state)
    return _response(state)


def _response(state: dict[str, Any]) -> dict[str, Any]:
    metadata = state.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    logs = state.get("logs", {})
    if isinstance(logs, dict):
        metadata = {**metadata, "logs": logs}
    return {
        "success": True,
        "sandbox_id": state.get("sandbox_id"),
        "provider": "local",
        "target": state.get("target"),
        "status": state.get("status", "unknown"),
        "endpoint": state.get("endpoint"),
        "metadata": metadata,
    }


def _canonical_target(target: str) -> str:
    normalized = target.strip().lower()
    if normalized in _BROWSER_TARGETS:
        return "browser"
    if normalized in _ANDROID_TARGETS:
        return "mobile"
    if normalized in _VM_TARGETS:
        raise LocalSandboxError(
            "the built-in local provider supports browser and Android targets only; "
            "configure a VM provider for PC/desktop targets"
        )
    raise LocalSandboxError("target must be one of browser/playwright or mobile/android")


def _validate_loopback_host(host: str) -> None:
    normalized = host.strip().strip("[]")
    if normalized.lower() == "localhost":
        return
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        raise LocalSandboxError("host must be localhost or a loopback IP address")
    if not address.is_loopback:
        raise LocalSandboxError("host must be localhost or a loopback IP address")


def _new_sandbox_id(target: str) -> str:
    return f"local-{target}-{int(time.time())}-{secrets.token_hex(4)}"


def _state_root() -> Path:
    override = os.environ.get("AGY_LOCAL_SANDBOX_STATE_DIR")
    root = Path(override).expanduser() if override else Path.home() / ".agy-mcp" / "local-sandboxes"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _state_path(sandbox_id: str) -> Path:
    if not _SANDBOX_ID_RE.fullmatch(sandbox_id):
        raise LocalSandboxError(f"sandbox id must match {_SANDBOX_ID_RE.pattern}")
    if sandbox_id in {".", ".."}:
        raise LocalSandboxError("sandbox id must not be '.' or '..'")
    return _state_root() / sandbox_id / "state.json"


def _load_state(sandbox_id: str) -> dict[str, Any]:
    path = _state_path(sandbox_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LocalSandboxError(f"local sandbox not found: {sandbox_id}")
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalSandboxError(f"local sandbox state is unreadable: {exc}")
    if not isinstance(data, dict):
        raise LocalSandboxError("local sandbox state is invalid")
    return data


def _write_state(state: dict[str, Any]) -> None:
    sandbox_id = str(state.get("sandbox_id", ""))
    path = _state_path(sandbox_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        raise LocalSandboxError(f"failed to write local sandbox state: {exc}")


def _refresh_state(state: dict[str, Any]) -> None:
    kind = state.get("kind")
    processes = state.get("processes", {}) if isinstance(state.get("processes"), dict) else {}
    metadata = state.get("metadata", {}) if isinstance(state.get("metadata"), dict) else {}
    if kind == "playwright":
        state["status"] = "running" if _process_running(_process_info(processes, "playwright")) else "stopped"
    elif kind == "android":
        serial = str(metadata.get("adb_serial", ""))
        adb = shutil.which("adb")
        online = bool(adb and serial and serial in _online_android_devices(adb))
        metadata["appium_running"] = _process_running(_process_info(processes, "appium"))
        metadata["adb_online"] = online
        state["metadata"] = metadata
        state["status"] = "running" if online else "stopped"
    state["updated_at"] = time.time()


def _playwright_command(cwd: Path) -> list[str] | None:
    override = os.environ.get("AGY_LOCAL_SANDBOX_PLAYWRIGHT_CMD")
    if override:
        command = shlex.split(override)
        return command or None

    local_bin = cwd / "node_modules" / ".bin" / ("playwright.cmd" if os.name == "nt" else "playwright")
    if local_bin.is_file():
        return [str(local_bin), "run-server"]

    playwright = shutil.which("playwright")
    if playwright:
        return [playwright, "run-server"]

    if importlib.util.find_spec("playwright") is not None:
        return [sys.executable, "-m", "playwright", "run-server"]

    npx = shutil.which("npx")
    if npx and _probe_command([npx, "--no-install", "playwright", "--version"], cwd=cwd):
        return [npx, "--no-install", "playwright", "run-server"]

    return None


def _first_android_avd(emulator: str) -> str:
    result = _run_capture([emulator, "-list-avds"], timeout=10)
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        avd = line.strip()
        if avd:
            return avd
    return ""


def _online_android_devices(adb: str) -> list[str]:
    result = _run_capture([adb, "devices"], timeout=10)
    if result.returncode != 0:
        return []
    devices: list[str] = []
    for raw_line in result.stdout.splitlines()[1:]:
        parts = raw_line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def _wait_for_android_device(
    adb: str,
    *,
    before: set[str],
    timeout: int,
    pid: int,
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        devices = _online_android_devices(adb)
        for serial in devices:
            if serial not in before:
                return serial
        if not _pid_running(pid):
            raise LocalSandboxError("Android emulator exited before adb reported an online device")
        time.sleep(1)
    raise LocalSandboxError("timed out waiting for Android emulator to appear in adb devices")


def _wait_for_android_boot(adb: str, *, serial: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _run_capture(
            [adb, "-s", serial, "shell", "getprop", "sys.boot_completed"],
            timeout=10,
        )
        if result.stdout.strip() == "1":
            return
        time.sleep(1)
    raise LocalSandboxError(f"timed out waiting for Android device {serial} to finish booting")


def _free_tcp_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _wait_for_tcp(
    host: str,
    port: int,
    timeout: int,
    *,
    pid: int | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid is not None and not _pid_running(pid):
            raise LocalSandboxError(f"process exited before opening {host}:{port}")
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.2)
    raise LocalSandboxError(f"timed out waiting for {host}:{port}")


def _spawn_detached(command: list[str], *, cwd: Path, log_path: Path) -> int:
    if os.name != "nt":
        return _fork_exec_detached(command, cwd=cwd, log_path=log_path)
    return _popen_detached_windows(command, cwd=cwd, log_path=log_path)


def _fork_exec_detached(command: list[str], *, cwd: Path, log_path: Path) -> int:
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise LocalSandboxError(f"failed to fork {Path(command[0]).name}: {exc}")

    if pid == 0:  # child
        os.close(read_fd)
        try:
            os.setsid()
            os.chdir(str(cwd))
            stdin_fd = os.open(os.devnull, os.O_RDONLY)
            log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.dup2(stdin_fd, 0)
                os.dup2(log_fd, 1)
                os.dup2(log_fd, 2)
            finally:
                if stdin_fd > 2:
                    os.close(stdin_fd)
                if log_fd > 2:
                    os.close(log_fd)
            os.execvpe(command[0], command, _child_environment())
        except BaseException as exc:  # noqa: BLE001 - last chance before os._exit.
            message = f"{type(exc).__name__}: {exc}".encode("utf-8", errors="replace")
            try:
                os.write(write_fd, message[:4096])
            except OSError:
                pass
            os._exit(127)

    os.close(write_fd)
    try:
        error = os.read(read_fd, 4096)
    finally:
        os.close(read_fd)
    if error:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        raise LocalSandboxError(
            f"failed to start {Path(command[0]).name}: "
            f"{error.decode('utf-8', errors='replace')}"
        )
    return pid


def _popen_detached_windows(command: list[str], *, cwd: Path, log_path: Path) -> int:
    log_handle = log_path.open("a", encoding="utf-8", errors="replace")
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": _child_environment(),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "text": True,
        "shell": False,
    }
    kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise LocalSandboxError(f"failed to start {Path(command[0]).name}: {exc}")
    finally:
        log_handle.close()
    _LIVE_PROCS[proc.pid] = proc
    return proc.pid


def _process_record(*, pid: int, command: list[str], log_path: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "pid": pid,
        "log": str(log_path),
        "command": Path(command[0]).name,
    }
    pgid = _process_group_id(pid)
    if pgid is not None:
        record["pgid"] = pgid
    start_token = _process_start_token(pid)
    if start_token:
        record["start_token"] = start_token
    elif os.name == "nt":
        record["identity_unverified"] = True
    return record


def _terminate_process(process: dict[str, Any] | None, *, allow_unverified: bool = False) -> bool:
    pid = _process_record_pid(process)
    if pid is None or not _pid_running(pid):
        return False
    if not allow_unverified and not _process_identity_matches(process):
        return False
    return _terminate_pid(pid, pgid=_process_record_pgid(process))


def _terminate_pid(pid: int, *, pgid: int | None = None) -> bool:
    if pid <= 0 or not _pid_running(pid):
        return False
    proc = _LIVE_PROCS.get(pid)
    try:
        if os.name == "nt":
            os.kill(pid, signal.SIGTERM)
        else:
            os.killpg(pgid or pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if proc is not None:
            try:
                proc.wait(timeout=0.1)
                _LIVE_PROCS.pop(pid, None)
                return True
            except subprocess.TimeoutExpired:
                continue
        if not _pid_running(pid):
            return True
        time.sleep(0.1)
    try:
        if os.name == "nt":
            os.kill(pid, signal.SIGKILL)
        else:
            os.killpg(pgid or pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    return True


def _pid_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            waited_pid, _ = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                return False
        except ChildProcessError:
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_info(processes: dict[str, Any], name: str) -> dict[str, Any] | None:
    raw = processes.get(name)
    return raw if isinstance(raw, dict) else None


def _process_running(process: dict[str, Any] | None) -> bool:
    pid = _process_record_pid(process)
    return bool(pid and _pid_running(pid) and _process_identity_matches(process))


def _process_identity_matches(process: dict[str, Any] | None) -> bool:
    pid = _process_record_pid(process)
    if pid is None:
        return False
    proc = _LIVE_PROCS.get(pid)
    if proc is not None and proc.poll() is None:
        return True
    expected = process.get("start_token") if isinstance(process, dict) else None
    if not isinstance(expected, str) or not expected:
        return bool(process.get("identity_unverified")) if isinstance(process, dict) else False
    return _process_start_token(pid) == expected


def _process_record_pid(process: dict[str, Any] | None) -> int | None:
    if not isinstance(process, dict):
        return None
    pid = process.get("pid")
    return int(pid) if isinstance(pid, int) and pid > 0 else None


def _process_record_pgid(process: dict[str, Any] | None) -> int | None:
    if not isinstance(process, dict):
        return None
    pgid = process.get("pgid")
    return int(pgid) if isinstance(pgid, int) and pgid > 0 else None


def _process_group_id(pid: int) -> int | None:
    if os.name == "nt":
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _process_start_token(pid: int) -> str:
    if os.name == "nt":
        return _windows_process_start_token(pid)
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        text = proc_stat.read_text(encoding="utf-8", errors="replace")
        fields = text.rsplit(") ", 1)[1].split()
        if len(fields) > 19:
            return f"proc:{fields[19]}"
    except (IndexError, OSError):
        pass
    result = _run_capture(["ps", "-p", str(pid), "-o", "lstart="], timeout=5)
    if result.returncode == 0 and result.stdout.strip():
        return f"ps:{result.stdout.strip()}"
    return ""


def _windows_process_start_token(pid: int) -> str:
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_Process -Filter "
            f"'ProcessId={pid}' | Select-Object -ExpandProperty CreationDate"
        ),
    ]
    result = _run_capture(command, timeout=5)
    if result.returncode == 0 and result.stdout.strip():
        return f"win:{result.stdout.strip()}"
    return ""


def _tail_log_files(state: dict[str, Any], *, tail: int) -> str:
    raw_logs = state.get("logs", {})
    if not isinstance(raw_logs, dict):
        return ""
    sections: list[str] = []
    for name, raw_path in sorted(raw_logs.items()):
        if not isinstance(raw_path, str):
            continue
        path = Path(raw_path)
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                lines = deque((line.rstrip("\n") for line in handle), maxlen=tail)
        except OSError:
            continue
        body = "\n".join(lines)
        sections.append(f"== {name} ==\n{body}" if body else f"== {name} ==")
    return "\n".join(sections)


def _child_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in _CHILD_ENV_ALLOWLIST or key.startswith("LC_")
    }


def _run_capture(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr="")


def _probe_command(command: list[str], *, cwd: Path) -> bool:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _run_quiet(command: list[str], *, timeout: int) -> None:
    try:
        subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _emit(result: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, sort_keys=True))
        return
    for key in ("sandbox_id", "status", "endpoint"):
        if result.get(key):
            print(f"{key}: {result[key]}")


def _emit_error(exc: Exception, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "success": False,
                    "status": "error",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return
    print(f"agy-local-sandbox failed: {exc}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
