"""Standalone doctor regressions that do not import the MCP server at collection."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path

import pytest


def test_main_reports_unhealthy_when_server_dependency_cannot_import(tmp_path: Path):
    dependency = tmp_path / "dependencies" / "mcp" / "server"
    dependency.mkdir(parents=True)
    (dependency.parent / "__init__.py").write_text("", encoding="utf-8")
    (dependency / "__init__.py").write_text("", encoding="utf-8")
    source = Path(__file__).resolve().parents[1] / "src"
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    session_root = tmp_path / "sessions"
    code = """
import os
from pathlib import Path
from agy_mcp import doctor
from agy_mcp.doctor import DoctorCheck

cfg = doctor.get_config()
assert cfg.source == os.environ["AGY_MCP_CONFIG"]
assert cfg.session_store_root() == Path(os.environ["AGY_MCP_SESSION_ROOT"])
doctor._check_uv = lambda safety: DoctorCheck("uv", True, "fixture ready")
doctor._check_backend = lambda adapter, safety, label: [
    DoctorCheck(f"{label}_binary", True, "fixture ready")
]
doctor._check_auth = lambda safety: DoctorCheck("auth", True, "fixture ready")
doctor._check_network_env = lambda safety: DoctorCheck("network_env", True, "fixture ready")
doctor._check_session_store = lambda *args, **kwargs: DoctorCheck(
    "session_store", True, "fixture ready"
)
try:
    import agy_mcp.server
except ModuleNotFoundError as exc:
    assert exc.name == "mcp.server.fastmcp"
else:
    raise AssertionError("the broken SDK fixture did not prevent server import")
raise SystemExit(doctor.main())
"""
    env = dict(os.environ)
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join([str(dependency.parents[1]), str(source)]),
        "AGY_MCP_CONFIG": str(config_path),
        "AGY_MCP_SESSION_ROOT": str(session_root),
    })
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    report = json.loads(result.stdout)

    assert config_path.read_text(encoding="utf-8") == ""
    assert not session_root.exists()
    assert report["healthy"] is False
    assert result.returncode == 1
    check = next(check for check in report["checks"] if check["name"] == "mcp_server")
    assert check["ok"] is False and check["severity"] == "error"
    assert "ModuleNotFoundError" in check["detail"]
    assert "mcp.server.fastmcp" in check["detail"]
    assert all(check["ok"] for check in report["checks"] if check["name"] != "mcp_server")


def _mcp_fixture(tmp_path: Path, code: str) -> Path:
    root = tmp_path / "dependencies"
    server = root / "mcp" / "server"
    server.mkdir(parents=True)
    (server.parent / "__init__.py").write_text("", encoding="utf-8")
    (server / "__init__.py").write_text("", encoding="utf-8")
    (server / "fastmcp.py").write_text(code, encoding="utf-8")
    return root


@pytest.fixture
def probe_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGY_MCP_CONFIG", str(config))
    monkeypatch.setenv("AGY_MCP_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    return tmp_path


def test_probe_redacts_before_persisting_and_keeps_import_diagnostic(
    probe_environment: Path, monkeypatch: pytest.MonkeyPatch, capsys,
):
    from agy_mcp import doctor
    from agy_mcp.config import SafetyConfig
    from agy_mcp.safety import SafetyPolicy

    message = (
        "cannot import name 'FastMCPCompat' from /home/fixture-user/sdk; "
        "Bearer synthetic-bearer-value; CUSTOM-IDENTITY; "
        + "x" * 150 + "; Bearer " + "synthetic-boundary-value" * 8
    )
    dependency = _mcp_fixture(probe_environment, f"""
import sys
print("import noise " * 100000)
print("stderr noise " * 100000, file=sys.stderr)
raise ImportError({message!r})
""")
    monkeypatch.setenv("PYTHONPATH", str(dependency) + os.pathsep + os.environ["PYTHONPATH"])
    run = subprocess.run
    persisted = []

    def inspect_diagnostic(argv, **kwargs):
        completed = run(argv, **kwargs)
        persisted.append(Path(argv[-1]).read_bytes())
        return completed

    monkeypatch.setattr(doctor.subprocess, "run", inspect_diagnostic)
    safety = SafetyPolicy(config=SafetyConfig(redact_extra_patterns=["CUSTOM-IDENTITY"]))
    check = doctor._check_mcp_server(safety)

    assert check.ok is False and check.severity == "error"
    assert "ImportError: cannot import name 'FastMCPCompat'" in check.detail
    assert "~/sdk" in check.detail
    assert "Python environment" in check.detail
    assert len(check.detail) <= 512
    assert len(persisted) == 1 and len(persisted[0]) <= 4096
    diagnostic = json.loads(persisted[0])
    assert diagnostic["ok"] is False and len(diagnostic["detail"]) <= 256
    for value in ("fixture-user", "synthetic-bearer", "CUSTOM-IDENTITY", "synthetic-boundary"):
        assert value not in diagnostic["detail"]
        assert value not in check.detail
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_successful_probe_registers_real_tools_without_initializing_state(
    probe_environment: Path,
):
    fixture = probe_environment / "startup"
    fixture.mkdir()
    forbidden = probe_environment / "forbidden-operation"
    registered = probe_environment / "registered.json"
    protected = [probe_environment / name for name in ("config.toml", "auth", "tasks", "memory")]
    for path in protected:
        path.write_text("fixture contents", encoding="utf-8")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected}
    (fixture / "sitecustomize.py").write_text(f"""
import json
import os
import socket
import sys
from pathlib import Path
from agy_mcp import config, session_store, supervisor
from agy_mcp.adapters import agy, base
from mcp.server.fastmcp import FastMCP

def deny(*args, **kwargs):
    Path({str(forbidden)!r}).write_text("unexpected initialization", encoding="utf-8")
    raise AssertionError("the import probe initialized state")

config.get_config = deny
session_store.SessionStore.__init__ = deny
supervisor.Supervisor.__init__ = deny
base.BaseAdapter.detect = deny
base.BaseAdapter.run = deny
agy.detect_agy_auth_source = deny
socket.socket.connect = deny
socket.socket.connect_ex = deny
protected = {set(map(str, protected))!r}
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        if os.fsdecode(args[0]) in protected:
            deny()
sys.addaudithook(audit)

original_tool = FastMCP.tool
names = []
def observed_tool(self, *args, **kwargs):
    decorate = original_tool(self, *args, **kwargs)
    def register(function):
        result = decorate(function)
        names.append(kwargs.get("name", function.__name__))
        Path({str(registered)!r}).write_text(json.dumps(names), encoding="utf-8")
        return result
    return register
FastMCP.tool = observed_tool
""", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(fixture) + os.pathsep + env["PYTHONPATH"]
    result = subprocess.run(
        [sys.executable, "-c", """
import json
from agy_mcp.doctor import _check_mcp_server
from agy_mcp.safety import SafetyPolicy
print(json.dumps(_check_mcp_server(SafetyPolicy()).to_dict()))
"""],
        capture_output=True, text=True, env=env, timeout=15,
    )
    check = json.loads(result.stdout)

    assert result.returncode == 0 and result.stderr == ""
    assert check["ok"] is True and check["severity"] == "info"
    assert {"agy", "agy_doctor", "agy_start"} <= set(json.loads(registered.read_text()))
    assert not forbidden.exists()
    assert not (probe_environment / "sessions").exists()
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected} == before


@pytest.mark.parametrize(
    "flags,optimization,environment_optimization",
    [
        ([], 0, None),
        (["-O"], 1, None),
        (["-OO"], 2, None),
        (["-E", "-O"], 1, None),
        (["-E", "-OO"], 2, None),
        ([], 1, "1"),
        ([], 2, "2"),
    ],
)
def test_public_doctor_preserves_caller_optimization(
    probe_environment: Path, flags: list[str], optimization: int,
    environment_optimization: str | None,
):
    package_root = _copy_probe_package(probe_environment)
    markers = probe_environment / "optimization-imports"
    oracle = probe_environment / "caller-oracle.json"
    dependency = _mcp_fixture(probe_environment, f'''
import os
from pathlib import Path

def optimization_level():
    """Keep this docstring to distinguish -O from -OO."""
    return 0 if __debug__ else (2 if optimization_level.__doc__ is None else 1)

level = optimization_level()
with Path({str(markers)!r}).open("a", encoding="utf-8") as output:
    output.write(str(level) + "\\n")
if level != int(os.environ["AGY_DOCTOR_TEST_OPTIMIZATION"]):
    raise ImportError("SDK imported with a different optimization level")

class FastMCP:
    def __init__(self, *args, **kwargs):
        pass
    def tool(self, *args, **kwargs):
        return lambda function: function
''')
    env = dict(os.environ)
    env.pop("PYTHONOPTIMIZE", None)
    env["PYTHONSAFEPATH"] = ""
    env["AGY_DOCTOR_TEST_OPTIMIZATION"] = str(optimization)
    if environment_optimization is not None:
        env["PYTHONOPTIMIZE"] = environment_optimization
    code = """
import json
import sys
from pathlib import Path
package_root, oracle = sys.argv[1:]
sys.path.insert(1, package_root)
from agy_mcp import doctor, server
from agy_mcp.doctor import DoctorCheck
Path(oracle).write_text(json.dumps({
    "optimize": sys.flags.optimize,
    "lazy_state": [server._config, server._store, server._supervisor],
}), encoding="utf-8")
doctor._check_uv = lambda safety: DoctorCheck("uv", True, "fixture")
doctor._check_backend = lambda *args, **kwargs: []
doctor._check_auth = lambda safety: DoctorCheck("auth", True, "fixture")
doctor._check_network_env = lambda safety: DoctorCheck("network_env", True, "fixture")
doctor._check_session_store = lambda *args, **kwargs: DoctorCheck("session_store", True, "fixture")
raise SystemExit(doctor.main())
"""
    result = subprocess.run(
        [sys.executable, "-B", *flags, "-c", code, str(package_root), str(oracle)],
        cwd=dependency, env=env, capture_output=True, text=True, timeout=15,
    )

    assert result.stderr == ""
    assert json.loads(oracle.read_text()) == {"optimize": optimization, "lazy_state": [None] * 3}
    report = json.loads(result.stdout)
    check = next(item for item in report["checks"] if item["name"] == "mcp_server")
    assert check["ok"] is True, check["detail"]
    assert report["healthy"] is True and result.returncode == 0
    assert markers.read_text().splitlines() == [str(optimization)] * 2
    assert (probe_environment / "config.toml").read_text(encoding="utf-8") == ""
    assert not (probe_environment / "sessions").exists()


@pytest.mark.parametrize(
    "flags,startup_warning,later_warning,caller_ok,probe_ok",
    [
        ([], "ignore", None, True, True),
        ([], "error", None, False, False),
        (["-W", "error"], "ignore", None, False, False),
        (["-Werror"], "ignore", None, False, False),
        (["-W", "ignore", "-W", "error"], "ignore", None, False, False),
        (["-W", "error", "-W", "ignore"], "error", None, True, True),
        (["-E", "-W", "error"], "ignore", None, False, False),
        ([], "ignore", "error", True, True),
        ([], "error", "ignore", False, False),
        ([], "", "error::DeprecationWarning", True, True),
        ([], "ignore::UserWarning", "error::DeprecationWarning", True, True),
        (["-Werror", "-Wignore"], "ignore,error", None, False, False),
        ([], "ignore,error", "error,ignore", False, False),
    ],
)
def test_public_doctor_preserves_parsed_startup_warning_options(
    probe_environment: Path, flags: list[str], startup_warning: str,
    later_warning: str | None, caller_ok: bool, probe_ok: bool,
):
    package_root = _copy_probe_package(probe_environment)
    oracle = probe_environment / "caller-oracle.json"
    markers = probe_environment / "warning-imports.jsonl"
    dependency = _mcp_fixture(probe_environment, f'''
import json
import sys
import warnings
from pathlib import Path
with Path({str(markers)!r}).open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.warnoptions) + "\\n")
warnings.warn("controlled SDK import warning", DeprecationWarning)
class FastMCP:
    def __init__(self, *args, **kwargs):
        pass
    def tool(self, *args, **kwargs):
        return lambda function: function
''')
    env = dict(os.environ)
    env["PYTHONSAFEPATH"] = ""
    env.pop("PYTHONDEVMODE", None)
    env["PYTHONWARNINGS"] = startup_warning
    code = """
import json
import os
import sys
from pathlib import Path
package_root, oracle, later_warning = sys.argv[1:]
sys.path.insert(1, package_root)
from agy_mcp import doctor
from agy_mcp.doctor import DoctorCheck
if later_warning != "unchanged":
    os.environ["PYTHONWARNINGS"] = later_warning
try:
    from agy_mcp import server
except DeprecationWarning as exc:
    caller_ok = False
    lazy_state = "agy_mcp.server" not in sys.modules
    error = str(exc)
else:
    caller_ok = True
    lazy_state = all(x is None for x in (server._config, server._store, server._supervisor))
    error = None
Path(oracle).write_text(json.dumps({
    "caller_ok": caller_ok, "lazy_state": lazy_state, "error": error,
    "warnoptions": sys.warnoptions,
}), encoding="utf-8")
doctor._check_uv = lambda safety: DoctorCheck("uv", True, "fixture")
doctor._check_backend = lambda *args, **kwargs: []
doctor._check_auth = lambda safety: DoctorCheck("auth", True, "fixture")
doctor._check_network_env = lambda safety: DoctorCheck("network_env", True, "fixture")
doctor._check_session_store = lambda *args, **kwargs: DoctorCheck("session_store", True, "fixture")
warning_environment = os.environ["PYTHONWARNINGS"]
exit_code = doctor.main()
assert os.environ["PYTHONWARNINGS"] == warning_environment
raise SystemExit(exit_code)
"""
    result = subprocess.run(
        [sys.executable, "-B", *flags, "-c", code, str(package_root), str(oracle),
         later_warning if later_warning is not None else "unchanged"],
        cwd=dependency, env=env, capture_output=True, text=True, timeout=15,
    )

    assert result.stderr == ""
    caller = json.loads(oracle.read_text())
    assert caller["caller_ok"] is caller_ok and caller["lazy_state"]
    if not caller_ok:
        assert caller["error"] == "controlled SDK import warning"
    report = json.loads(result.stdout)
    check = next(item for item in report["checks"] if item["name"] == "mcp_server")
    assert check["ok"] is probe_ok, check["detail"]
    assert report["healthy"] is probe_ok and result.returncode == (0 if probe_ok else 1)
    if not probe_ok:
        assert "DeprecationWarning: controlled SDK import warning" in check["detail"]
    imports = [json.loads(line) for line in markers.read_text().splitlines()]
    assert len(imports) == 2 and imports == [caller["warnoptions"]] * 2
    assert (probe_environment / "config.toml").read_text(encoding="utf-8") == ""
    assert not (probe_environment / "sessions").exists()


@pytest.mark.parametrize(
    "flags,bytes_mode,caller_ok",
    [
        ([], 0, True),
        (["-b"], 1, True),
        (["-bb"], 2, False),
        (["-b", "-W", "error::BytesWarning"], 1, True),
        (["-b", "-W", "default::BytesWarning", "-W", "error::BytesWarning"], 1, False),
        (["-bb", "-W", "error::BytesWarning", "-W", "ignore::BytesWarning"], 2, True),
    ],
)
def test_public_doctor_preserves_bytes_warning_generation_and_filter_order(
    probe_environment: Path, flags: list[str], bytes_mode: int, caller_ok: bool,
):
    package_root = _copy_probe_package(probe_environment)
    oracle = probe_environment / "caller-oracle.json"
    markers = probe_environment / "bytes-warning-imports.jsonl"
    dependency = _mcp_fixture(probe_environment, f'''
import json
import sys
from pathlib import Path
with Path({str(markers)!r}).open("a", encoding="utf-8") as output:
    output.write(json.dumps({{"bytes_warning": sys.flags.bytes_warning,
                            "warnoptions": sys.warnoptions}}) + "\\n")
comparison = b"controlled SDK bytes" == "controlled SDK bytes"
class FastMCP:
    def __init__(self, *args, **kwargs):
        pass
    def tool(self, *args, **kwargs):
        return lambda function: function
''')
    env = dict(os.environ)
    env["PYTHONSAFEPATH"] = ""
    env.pop("PYTHONDEVMODE", None)
    env["PYTHONWARNINGS"] = "ignore::UserWarning"
    code = """
import json
import os
import sys
from pathlib import Path
package_root, oracle = sys.argv[1:]
sys.path.insert(1, package_root)
from agy_mcp import doctor
from agy_mcp.doctor import DoctorCheck
try:
    from agy_mcp import server
except BytesWarning as exc:
    caller_ok = False
    lazy_state = "agy_mcp.server" not in sys.modules
    error = str(exc)
else:
    caller_ok = True
    lazy_state = all(x is None for x in (server._config, server._store, server._supervisor))
    error = None
Path(oracle).write_text(json.dumps({
    "caller_ok": caller_ok, "lazy_state": lazy_state, "error": error,
    "bytes_warning": sys.flags.bytes_warning, "warnoptions": sys.warnoptions,
}), encoding="utf-8")
doctor._check_uv = lambda safety: DoctorCheck("uv", True, "fixture")
doctor._check_backend = lambda *args, **kwargs: []
doctor._check_auth = lambda safety: DoctorCheck("auth", True, "fixture")
doctor._check_network_env = lambda safety: DoctorCheck("network_env", True, "fixture")
doctor._check_session_store = lambda *args, **kwargs: DoctorCheck("session_store", True, "fixture")
warning_environment = os.environ["PYTHONWARNINGS"]
exit_code = doctor.main()
assert os.environ["PYTHONWARNINGS"] == warning_environment
raise SystemExit(exit_code)
"""
    result = subprocess.run(
        [sys.executable, "-B", *flags, "-c", code, str(package_root), str(oracle)],
        cwd=dependency, env=env, capture_output=True, text=True, timeout=15,
    )

    caller = json.loads(oracle.read_text())
    assert caller["caller_ok"] is caller_ok and caller["lazy_state"]
    assert caller["bytes_warning"] == bytes_mode
    if flags in (["-b"], ["-b", "-W", "error::BytesWarning"]):
        assert "BytesWarning: Comparison between bytes and string" in result.stderr
    else:
        assert result.stderr == ""
    report = json.loads(result.stdout)
    check = next(item for item in report["checks"] if item["name"] == "mcp_server")
    assert check["ok"] is caller_ok, check["detail"]
    assert report["healthy"] is caller_ok and result.returncode == (0 if caller_ok else 1)
    if not caller_ok:
        assert "BytesWarning: Comparison between bytes and string" in check["detail"]
    imports = [json.loads(line) for line in markers.read_text().splitlines()]
    assert len(imports) == 2
    assert [item["warnoptions"] for item in imports] == [caller["warnoptions"]] * 2
    assert [item["bytes_warning"] for item in imports] == [bytes_mode] * 2
    assert (probe_environment / "config.toml").read_text(encoding="utf-8") == ""
    assert not (probe_environment / "sessions").exists()


@pytest.mark.parametrize(
    "flags,encoding_mode,later_encoding,caller_ok",
    [
        (["-W", "error::EncodingWarning"], 0, None, True),
        (["-X", "warn_default_encoding", "-W", "error::EncodingWarning"], 1, None, False),
        (["-W", "error::EncodingWarning"], 0, "1", True),
    ],
)
def test_public_doctor_preserves_startup_encoding_warning_generation(
    probe_environment: Path, flags: list[str], encoding_mode: int,
    later_encoding: str | None, caller_ok: bool,
):
    package_root = _copy_probe_package(probe_environment)
    oracle = probe_environment / "caller-oracle.json"
    markers = probe_environment / "encoding-warning-imports.jsonl"
    ascii_file = probe_environment / "owned-ascii.txt"
    ascii_file.write_text("owned ASCII contents", encoding="utf-8")
    dependency = _mcp_fixture(probe_environment, f'''
import json
import os
import sys
from pathlib import Path
with Path({str(markers)!r}).open("a", encoding="utf-8") as output:
    output.write(json.dumps({{"warn_default_encoding": sys.flags.warn_default_encoding,
                            "warnoptions": sys.warnoptions,
                            "encoding_env_present": "PYTHONWARNDEFAULTENCODING" in os.environ}}) + "\\n")
with open({str(ascii_file)!r}) as input_file:
    contents = input_file.read()
class FastMCP:
    def __init__(self, *args, **kwargs):
        pass
    def tool(self, *args, **kwargs):
        return lambda function: function
''')
    env = dict(os.environ)
    env["PYTHONSAFEPATH"] = ""
    env.pop("PYTHONDEVMODE", None)
    env.pop("PYTHONWARNDEFAULTENCODING", None)
    env["PYTHONWARNINGS"] = "ignore::UserWarning"
    code = """
import json
import os
import sys
from pathlib import Path
package_root, oracle, later_encoding = sys.argv[1:]
sys.path.insert(1, package_root)
from agy_mcp import doctor
from agy_mcp.doctor import DoctorCheck
if later_encoding != "unchanged":
    os.environ["PYTHONWARNDEFAULTENCODING"] = later_encoding
try:
    from agy_mcp import server
except EncodingWarning as exc:
    caller_ok = False
    lazy_state = "agy_mcp.server" not in sys.modules
    error = str(exc)
else:
    caller_ok = True
    lazy_state = all(x is None for x in (server._config, server._store, server._supervisor))
    error = None
Path(oracle).write_text(json.dumps({
    "caller_ok": caller_ok, "lazy_state": lazy_state, "error": error,
    "warn_default_encoding": sys.flags.warn_default_encoding, "warnoptions": sys.warnoptions,
}), encoding="utf-8")
doctor._check_uv = lambda safety: DoctorCheck("uv", True, "fixture")
doctor._check_backend = lambda *args, **kwargs: []
doctor._check_auth = lambda safety: DoctorCheck("auth", True, "fixture")
doctor._check_network_env = lambda safety: DoctorCheck("network_env", True, "fixture")
doctor._check_session_store = lambda *args, **kwargs: DoctorCheck("session_store", True, "fixture")
warning_environment = os.environ["PYTHONWARNINGS"]
encoding_environment = os.environ.get("PYTHONWARNDEFAULTENCODING")
exit_code = doctor.main()
assert os.environ["PYTHONWARNINGS"] == warning_environment
assert os.environ.get("PYTHONWARNDEFAULTENCODING") == encoding_environment
raise SystemExit(exit_code)
"""
    result = subprocess.run(
        [sys.executable, "-B", *flags, "-c", code, str(package_root), str(oracle),
         later_encoding if later_encoding is not None else "unchanged"],
        cwd=dependency, env=env, capture_output=True, text=True, timeout=15,
    )

    assert result.stderr == ""
    caller = json.loads(oracle.read_text())
    assert caller["caller_ok"] is caller_ok and caller["lazy_state"]
    assert caller["warn_default_encoding"] == encoding_mode
    report = json.loads(result.stdout)
    check = next(item for item in report["checks"] if item["name"] == "mcp_server")
    assert check["ok"] is caller_ok, check["detail"]
    assert report["healthy"] is caller_ok and result.returncode == (0 if caller_ok else 1)
    if not caller_ok:
        assert "EncodingWarning" in check["detail"] and "encoding" in caller["error"]
    imports = [json.loads(line) for line in markers.read_text().splitlines()]
    assert len(imports) == 2
    assert [item["warnoptions"] for item in imports] == [caller["warnoptions"]] * 2
    assert [item["warn_default_encoding"] for item in imports] == [encoding_mode] * 2
    assert [item["encoding_env_present"] for item in imports] == [later_encoding is not None, False]
    assert ascii_file.read_text(encoding="utf-8") == "owned ASCII contents"
    assert (probe_environment / "config.toml").read_text(encoding="utf-8") == ""
    assert not (probe_environment / "sessions").exists()


@pytest.mark.parametrize(
    "operation,flags,environment_mode,effective_mode,caller_ok",
    [
        ("utf8", ["-X", "utf8=0"], "1", 0, False),
        ("utf8", ["-X", "utf8=1"], "0", 1, True),
        ("integer", ["-X", "int_max_str_digits=640"], "0", 640, False),
        ("integer", ["-X", "int_max_str_digits=0"], "640", 0, True),
    ],
)
def test_public_doctor_preserves_effective_text_and_integer_conversion_modes(
    probe_environment: Path, operation: str, flags: list[str],
    environment_mode: str, effective_mode: int, caller_ok: bool,
):
    package_root = _copy_probe_package(probe_environment)
    oracle = probe_environment / "caller-oracle.json"
    markers = probe_environment / "builtin-imports.jsonl"
    text_file = probe_environment / "owned-utf8-digit.txt"
    text_file.write_text("\u0661", encoding="utf-8")
    dependency = _mcp_fixture(probe_environment, f'''
import json
import os
import sys
from pathlib import Path
with Path({str(markers)!r}).open("a", encoding="utf-8") as output:
    output.write(json.dumps({{"utf8": sys.flags.utf8_mode,
                            "integer": sys.get_int_max_str_digits(),
                            "raw_utf8_env": os.environ.get("PYTHONUTF8"),
                            "raw_integer_env": os.environ.get("PYTHONINTMAXSTRDIGITS")}}) + "\\n")
if {operation!r} == "utf8":
    with open({str(text_file)!r}) as input_file:
        value = int(input_file.read())
else:
    value = int("1" * 700)
class FastMCP:
    def __init__(self, *args, **kwargs):
        pass
    def tool(self, *args, **kwargs):
        return lambda function: function
''')
    env = dict(os.environ)
    env["PYTHONSAFEPATH"] = ""
    for name in ("PYTHONUTF8", "PYTHONINTMAXSTRDIGITS", "PYTHONWARNDEFAULTENCODING", "PYTHONDEVMODE"):
        env.pop(name, None)
    env.update({"LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONWARNINGS": "ignore"})
    env["PYTHONUTF8" if operation == "utf8" else "PYTHONINTMAXSTRDIGITS"] = environment_mode
    code = """
import json
import os
import sys
from pathlib import Path
package_root, oracle = sys.argv[1:]
sys.path.insert(1, package_root)
from agy_mcp import doctor
from agy_mcp.doctor import DoctorCheck
try:
    from agy_mcp import server
except (UnicodeDecodeError, ValueError) as exc:
    caller_ok = False
    lazy_state = "agy_mcp.server" not in sys.modules
    error = str(exc)
else:
    caller_ok = True
    lazy_state = all(x is None for x in (server._config, server._store, server._supervisor))
    error = None
Path(oracle).write_text(json.dumps({
    "caller_ok": caller_ok, "lazy_state": lazy_state, "error": error,
    "utf8": sys.flags.utf8_mode, "integer": sys.get_int_max_str_digits(),
}), encoding="utf-8")
doctor._check_uv = lambda safety: DoctorCheck("uv", True, "fixture")
doctor._check_backend = lambda *args, **kwargs: []
doctor._check_auth = lambda safety: DoctorCheck("auth", True, "fixture")
doctor._check_network_env = lambda safety: DoctorCheck("network_env", True, "fixture")
doctor._check_session_store = lambda *args, **kwargs: DoctorCheck("session_store", True, "fixture")
environment = {key: os.environ.get(key) for key in (
    "PYTHONUTF8", "PYTHONINTMAXSTRDIGITS", "PYTHONWARNINGS", "PYTHONPATH"
)}
exit_code = doctor.main()
assert {key: os.environ.get(key) for key in environment} == environment
raise SystemExit(exit_code)
"""
    result = subprocess.run(
        [sys.executable, "-B", *flags, "-c", code, str(package_root), str(oracle)],
        cwd=dependency, env=env, capture_output=True, text=True, timeout=15,
    )

    assert result.stderr == ""
    caller = json.loads(oracle.read_text())
    assert caller["caller_ok"] is caller_ok and caller["lazy_state"]
    assert caller[operation] == effective_mode
    report = json.loads(result.stdout)
    check = next(item for item in report["checks"] if item["name"] == "mcp_server")
    assert check["ok"] is caller_ok, check["detail"]
    assert report["healthy"] is caller_ok and result.returncode == (0 if caller_ok else 1)
    if not caller_ok:
        assert "UnicodeDecodeError" in check["detail"] or "ValueError" in check["detail"]
    imports = [json.loads(line) for line in markers.read_text().splitlines()]
    assert len(imports) == 2
    assert [item[operation] for item in imports] == [effective_mode] * 2
    assert [item["raw_utf8_env"] for item in imports] == [env.get("PYTHONUTF8")] * 2
    assert [item["raw_integer_env"] for item in imports] == [env.get("PYTHONINTMAXSTRDIGITS")] * 2
    assert text_file.read_text(encoding="utf-8") == "\u0661"
    assert (probe_environment / "config.toml").read_text(encoding="utf-8") == ""
    assert not (probe_environment / "sessions").exists()


@pytest.mark.parametrize(
    "flags,disable_at_runtime,disabled,explicit_prefix",
    [
        pytest.param(["-B"], False, True, False, id="explicit_B"),
        pytest.param([], True, True, False, id="runtime_disabled"),
        pytest.param([], False, False, False, id="normal_write_control"),
        pytest.param([], False, False, True, id="explicit_cache_prefix"),
    ],
)
def test_public_doctor_preserves_caller_bytecode_cache_policy(
    probe_environment: Path, flags: list[str], disable_at_runtime: bool,
    disabled: bool, explicit_prefix: bool,
):
    package_root = _copy_probe_package(probe_environment)
    markers = probe_environment / "bytecode-imports.jsonl"
    oracle = probe_environment / "caller-oracle.json"
    cache_prefix = probe_environment / "bytecode-cache"
    fallback_prefix = probe_environment / "fallback-bytecode-cache"
    dependency = _mcp_fixture(probe_environment, f'''
import json
import sys
from pathlib import Path
with Path({str(markers)!r}).open("a", encoding="utf-8") as output:
    output.write(json.dumps({{"disabled": sys.dont_write_bytecode,
                             "prefix": sys.pycache_prefix}}) + "\\n")
class FastMCP:
    def __init__(self, *args, **kwargs):
        pass
    def tool(self, *args, **kwargs):
        return lambda function: function
''')
    env = dict(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env["PYTHONPYCACHEPREFIX"] = str(fallback_prefix if explicit_prefix else cache_prefix)
    env["PYTHONSAFEPATH"] = ""
    code = """
import json
import os
import sys
from pathlib import Path
package_root, oracle, disable_at_runtime = sys.argv[1:]
sys.path.insert(1, package_root)
from agy_mcp import doctor
from agy_mcp.doctor import DoctorCheck
if disable_at_runtime == "True":
    sys.dont_write_bytecode = True
from agy_mcp import server
prefix = Path(sys.pycache_prefix)
Path(oracle).write_text(json.dumps({
    "disabled": sys.dont_write_bytecode,
    "cache_before": sorted(str(path) for path in prefix.rglob("*.pyc")),
    "lazy_state": [server._config, server._store, server._supervisor],
}), encoding="utf-8")
doctor._check_uv = lambda safety: DoctorCheck("uv", True, "fixture")
doctor._check_backend = lambda *args, **kwargs: []
doctor._check_auth = lambda safety: DoctorCheck("auth", True, "fixture")
doctor._check_network_env = lambda safety: DoctorCheck("network_env", True, "fixture")
doctor._check_session_store = lambda *args, **kwargs: DoctorCheck("session_store", True, "fixture")
environment = dict(os.environ)
exit_code = doctor.main()
assert dict(os.environ) == environment
raise SystemExit(exit_code)
"""
    caller_flags = [*flags]
    if explicit_prefix:
        caller_flags.extend(["-X", f"pycache_prefix={cache_prefix}"])
    result = subprocess.run(
        [sys.executable, *caller_flags, "-c", code, str(package_root), str(oracle), str(disable_at_runtime)],
        cwd=dependency, env=env, capture_output=True, text=True, timeout=15,
    )

    assert result.returncode == 0 and result.stderr == ""
    caller = json.loads(oracle.read_text(encoding="utf-8"))
    assert caller["disabled"] is disabled and caller["lazy_state"] == [None] * 3
    report = json.loads(result.stdout)
    assert report["healthy"] and all(check["ok"] for check in report["checks"])
    caches = {str(path) for path in cache_prefix.rglob("*.pyc")}
    if disabled:
        assert caches == set(caller["cache_before"])
    else:
        assert caches
    assert not fallback_prefix.exists()
    assert [json.loads(line) for line in markers.read_text().splitlines()] == [
        {"disabled": disabled, "prefix": str(cache_prefix)}
    ] * 2
    assert (probe_environment / "config.toml").read_text(encoding="utf-8") == ""
    assert not (probe_environment / "sessions").exists()


def test_timeout_kills_and_reaps_direct_child_and_removes_diagnostic_directory(
    probe_environment: Path, monkeypatch: pytest.MonkeyPatch,
):
    from agy_mcp import doctor
    from agy_mcp.safety import SafetyPolicy

    pid_file = probe_environment / "import-pid"
    dependency = _mcp_fixture(probe_environment, f"""
import os
import time
from pathlib import Path
Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(60)
""")
    monkeypatch.setenv("PYTHONPATH", str(dependency) + os.pathsep + os.environ["PYTHONPATH"])
    monkeypatch.setattr(doctor, "_SERVER_IMPORT_TIMEOUT", 1)
    popen = subprocess.Popen
    children = []
    directories = []

    def observe_child(argv, **kwargs):
        directories.append(Path(argv[-1]).parent)
        process = popen(argv, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(doctor.subprocess, "Popen", observe_child)
    started = time.monotonic()
    check = doctor._check_mcp_server(SafetyPolicy())
    elapsed = time.monotonic() - started

    assert check.ok is False and check.severity == "error"
    assert "timed out after 1 seconds" in check.detail
    assert 1 <= elapsed < 10
    assert len(children) == 1 and int(pid_file.read_text()) == children[0].pid
    assert children[0].returncode is not None and children[0].returncode != 0
    if os.name == "posix":
        with pytest.raises(ChildProcessError):
            os.waitpid(children[0].pid, os.WNOHANG)
    assert all(not path.exists() for path in directories)


def test_probe_reports_redacted_spawn_failure_and_cleans_temporary_directory(
    probe_environment: Path, monkeypatch: pytest.MonkeyPatch,
):
    from agy_mcp import doctor
    from agy_mcp.safety import SafetyPolicy

    directories = []
    def fail_start(argv, **kwargs):
        directories.append(Path(argv[-1]).parent)
        raise FileNotFoundError("/home/fixture-user/python Bearer synthetic-launch-value")

    monkeypatch.setattr(doctor.subprocess, "run", fail_start)
    check = doctor._check_mcp_server(SafetyPolicy())

    assert check.ok is False and check.severity == "error"
    assert "could not start" in check.detail and "~/python" in check.detail
    assert "fixture-user" not in check.detail and "synthetic-launch-value" not in check.detail
    assert len(directories) == 1 and not directories[0].exists()


@pytest.mark.parametrize("exit_code", [0, 17])
def test_probe_requires_import_completion_even_when_child_exits(
    probe_environment: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int,
):
    from agy_mcp import doctor
    from agy_mcp.safety import SafetyPolicy

    dependency = _mcp_fixture(probe_environment, f"import os\nos._exit({exit_code})\n")
    monkeypatch.setenv("PYTHONPATH", str(dependency) + os.pathsep + os.environ["PYTHONPATH"])
    check = doctor._check_mcp_server(SafetyPolicy())

    assert check.ok is False and check.severity == "error"
    if exit_code:
        assert "exited with code 17" in check.detail
    else:
        assert "did not produce a valid completion diagnostic" in check.detail


def test_probe_reports_tool_registration_exception(
    probe_environment: Path, monkeypatch: pytest.MonkeyPatch,
):
    from agy_mcp import doctor
    from agy_mcp.safety import SafetyPolicy

    dependency = _mcp_fixture(probe_environment, """
class FastMCP:
    def __init__(self, *args, **kwargs):
        pass
    def tool(self, *args, **kwargs):
        def register(function):
            raise RuntimeError("cannot register tool schema for " + kwargs["name"])
        return register
""")
    monkeypatch.setenv("PYTHONPATH", str(dependency) + os.pathsep + os.environ["PYTHONPATH"])
    check = doctor._check_mcp_server(SafetyPolicy())

    assert check.ok is False and check.severity == "error"
    assert "RuntimeError: cannot register tool schema for agy" in check.detail


def test_probe_reimports_when_server_is_already_cached_in_caller(probe_environment: Path):
    dependency = _mcp_fixture(probe_environment, "raise ImportError('changed dependency')\n")
    result = subprocess.run(
        [sys.executable, "-c", f"""
import json
import os
from agy_mcp import doctor, server
from agy_mcp.safety import SafetyPolicy
assert server._config is None and server._store is None and server._supervisor is None
os.environ["PYTHONPATH"] = {str(dependency)!r} + os.pathsep + os.environ["PYTHONPATH"]
check = doctor._check_mcp_server(SafetyPolicy())
assert server._config is None and server._store is None and server._supervisor is None
print(json.dumps(check.to_dict()))
"""],
        capture_output=True, text=True, timeout=15,
    )
    check = json.loads(result.stdout)

    assert result.returncode == 0 and result.stderr == ""
    assert check["ok"] is False and check["severity"] == "error"
    assert "ImportError: changed dependency" in check["detail"]


@pytest.mark.parametrize("payload", [b"invalid JSON", b'{"ok": "true"}', b" " * 4097])
def test_probe_rejects_invalid_or_oversized_diagnostics(
    probe_environment: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes,
):
    from agy_mcp import doctor
    from agy_mcp.safety import SafetyPolicy

    def produce_diagnostic(argv, **kwargs):
        Path(argv[-1]).write_bytes(payload)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(doctor.subprocess, "run", produce_diagnostic)
    check = doctor._check_mcp_server(SafetyPolicy())

    assert check.ok is False and check.severity == "error"
    assert "did not produce a valid completion diagnostic" in check.detail


@pytest.mark.parametrize("safe_path", [False, True])
def test_probe_uses_current_package_and_preserves_explicit_dependency_path(
    probe_environment: Path, safe_path: bool,
):
    dependency = _mcp_fixture(
        probe_environment, "raise ImportError('explicit dependency fixture')\n",
    )
    shadow = probe_environment / "cwd" / "agy_mcp"
    shadow.mkdir(parents=True)
    marker = probe_environment / "wrong-package-imported"
    (shadow / "__init__.py").write_text(f"""
from pathlib import Path
Path({str(marker)!r}).write_text("wrong package", encoding="utf-8")
raise ImportError("wrong agy-mcp package")
""", encoding="utf-8")
    source = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(dependency), str(source)])
    env["PYTHONSAFEPATH"] = "1" if safe_path else ""
    result = subprocess.run(
        [sys.executable, "-c", f"""
import sys
if not sys.flags.safe_path:
    sys.path[0] = {str(source)!r}
import json
from agy_mcp.doctor import _check_mcp_server
from agy_mcp.safety import SafetyPolicy
print(json.dumps(_check_mcp_server(SafetyPolicy()).to_dict()))
"""],
        capture_output=True, text=True, env=env, cwd=shadow.parent, timeout=15,
    )
    check = json.loads(result.stdout)

    assert result.returncode == 0 and result.stderr == ""
    assert check["ok"] is False and check["severity"] == "error"
    assert "ImportError: explicit dependency fixture" in check["detail"]
    assert not marker.exists()


@pytest.mark.parametrize("safe_path", [False, True])
def test_probe_preserves_dependency_priority_in_installed_package_layout(
    probe_environment: Path, safe_path: bool,
):
    dependency = _mcp_fixture(probe_environment, "raise ImportError('explicit broken SDK')\n")
    installed = probe_environment / "site-packages"
    source = Path(__file__).resolve().parents[1] / "src" / "agy_mcp"
    mcp_source = Path(importlib.util.find_spec("mcp").origin).parent
    for package, destination in ((source, "agy_mcp"), (mcp_source, "mcp")):
        shutil.copytree(
            package, installed / destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(dependency), str(installed)])
    env["PYTHONSAFEPATH"] = "1" if safe_path else ""
    result = subprocess.run(
        [sys.executable, "-c", """
import json
from agy_mcp import doctor
from agy_mcp.safety import SafetyPolicy
try:
    import agy_mcp.server
except ImportError as exc:
    assert str(exc) == "explicit broken SDK"
else:
    raise AssertionError("the caller's dependency fixture did not prevent server import")
print(json.dumps(doctor._check_mcp_server(SafetyPolicy()).to_dict()))
"""],
        capture_output=True, text=True, env=env, timeout=15,
    )
    check = json.loads(result.stdout)

    assert result.returncode == 0 and result.stderr == ""
    assert check["ok"] is False and check["severity"] == "error"
    assert "ImportError: explicit broken SDK" in check["detail"]


@pytest.mark.parametrize("mode", ["command", "module", "console"])
@pytest.mark.parametrize("safe_path", [False, True])
def test_probe_matches_entrypoint_dependency_search(
    probe_environment: Path, mode: str, safe_path: bool,
):
    cwd = _mcp_fixture(probe_environment, "raise ImportError('cwd-only broken SDK')\n")
    caller = probe_environment / "entrypoint"
    caller.mkdir()
    code = """
import json
from agy_mcp import doctor
from agy_mcp.safety import SafetyPolicy
try:
    import agy_mcp.server
except ImportError as exc:
    assert str(exc) == "cwd-only broken SDK"
    caller_failed = True
else:
    caller_failed = False
    assert agy_mcp.server._config is None
    assert agy_mcp.server._store is None
    assert agy_mcp.server._supervisor is None
print(json.dumps({
    "caller_failed": caller_failed,
    "probe": doctor._check_mcp_server(SafetyPolicy()).to_dict(),
}))
"""
    env = dict(os.environ)
    env["PYTHONSAFEPATH"] = ""
    env["PYTHONPATH"] = str(caller) + os.pathsep + env["PYTHONPATH"]
    argv = [sys.executable, *(["-P"] if safe_path else [])]
    if mode == "command":
        argv.extend(["-c", code])
    elif mode == "module":
        (caller / "doctor_path_caller.py").write_text(code, encoding="utf-8")
        argv.extend(["-m", "doctor_path_caller"])
    else:
        script = caller / "agy-doctor"
        script.write_text(code, encoding="utf-8")
        argv.append(str(script))
    result = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=cwd, timeout=15)
    data = json.loads(result.stdout)

    assert result.returncode == 0 and result.stderr == ""
    expected_failure = mode != "console" and not safe_path
    assert data["caller_failed"] is expected_failure
    assert data["probe"]["ok"] is not expected_failure
    if expected_failure:
        assert "ImportError: cwd-only broken SDK" in data["probe"]["detail"]
    assert (probe_environment / "config.toml").read_text(encoding="utf-8") == ""
    assert not (probe_environment / "sessions").exists()


def test_safe_path_probe_preserves_new_override_with_adjacent_installed_sdk(
    probe_environment: Path,
):
    dependency = _mcp_fixture(probe_environment, "raise ImportError('new broken SDK override')\n")
    installed = probe_environment / "site-packages"
    source = Path(__file__).resolve().parents[1] / "src" / "agy_mcp"
    mcp_source = Path(importlib.util.find_spec("mcp").origin).parent
    for package, destination in ((source, "agy_mcp"), (mcp_source, "mcp")):
        shutil.copytree(
            package, installed / destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    env = dict(os.environ)
    env["PYTHONSAFEPATH"] = ""
    env["PYTHONPATH"] = str(installed)
    result = subprocess.run(
        [sys.executable, "-P", "-c", f"""
import json
import os
import sys
from agy_mcp import doctor, server
from agy_mcp.safety import SafetyPolicy
assert sys.flags.safe_path and sys.path[0] == {str(installed)!r}
assert server._config is None and server._store is None and server._supervisor is None
os.environ["PYTHONPATH"] = {str(dependency)!r} + os.pathsep + os.environ["PYTHONPATH"]
print(json.dumps(doctor._check_mcp_server(SafetyPolicy()).to_dict()))
"""],
        capture_output=True, text=True, env=env, timeout=15,
    )
    check = json.loads(result.stdout)

    assert result.returncode == 0 and result.stderr == ""
    assert check["ok"] is False and check["severity"] == "error"
    assert "ImportError: new broken SDK override" in check["detail"]


def _copy_probe_package(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "src" / "agy_mcp"
    package_root = tmp_path / "package"
    shutil.copytree(
        source, package_root / "agy_mcp",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return package_root


@pytest.mark.parametrize("flag", ["-E", "-I"])
def test_probe_preserves_caller_environment_isolation(probe_environment: Path, flag: str):
    package_root = _copy_probe_package(probe_environment)
    dependency = _mcp_fixture(
        probe_environment, "raise ImportError('SDK from excluded PYTHONPATH')\n",
    )
    marker = probe_environment / "excluded-sdk-imported"
    (dependency / "mcp" / "__init__.py").write_text(f"""
from pathlib import Path
Path({str(marker)!r}).write_text("imported", encoding="utf-8")
""", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(dependency)
    env["PYTHONSAFEPATH"] = ""
    result = subprocess.run(
        [sys.executable, "-B", flag, "-c", """
import json
import sys
from pathlib import Path
package_root, flag, marker = sys.argv[1:]
assert sys.flags.ignore_environment
assert bool(sys.flags.isolated) == (flag == "-I")
# Bootstrap this package without adding excluded SDK paths.
sys.path.insert(1 if not sys.flags.safe_path else 0, package_root)
from agy_mcp import doctor, server
from agy_mcp.safety import SafetyPolicy
assert not Path(marker).exists()
assert server._config is None and server._store is None and server._supervisor is None
print(json.dumps(doctor._check_mcp_server(SafetyPolicy()).to_dict()))
""", str(package_root), flag, str(marker)],
        capture_output=True, text=True, env=env, cwd=probe_environment, timeout=15,
    )
    assert result.returncode == 0 and result.stderr == ""
    check = json.loads(result.stdout)

    assert check["ok"] is True
    assert not marker.exists()
    assert (probe_environment / "config.toml").read_text(encoding="utf-8") == ""
    assert not (probe_environment / "sessions").exists()


@pytest.mark.parametrize("flag", ["normal", "-s", "-S"])
def test_probe_preserves_caller_site_exclusions(probe_environment: Path, flag: str):
    package_root = _copy_probe_package(probe_environment)
    runtime = probe_environment / "interpreter"
    venv.EnvBuilder(with_pip=False, system_site_packages=True).create(runtime)
    executable = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    dependency = _mcp_fixture(probe_environment, """
import agy_doctor_user_site_fixture
class FastMCP:
    def __init__(self, *args, **kwargs):
        pass
    def tool(self, *args, **kwargs):
        return lambda function: function
""")
    dependencies = Path(importlib.util.find_spec("pydantic").origin).parent.parent
    userbase = probe_environment / "userbase"
    marker = probe_environment / "user-site-imported"
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": os.pathsep.join([str(dependency), str(package_root), str(dependencies)]),
        "PYTHONUSERBASE": str(userbase),
        "PYTHONNOUSERSITE": "",
        "PYTHONSAFEPATH": "",
    })
    location = subprocess.run(
        [str(executable), "-B", "-S", "-c", "import site; print(site.getusersitepackages())"],
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert location.returncode == 0 and location.stderr == ""
    user_site = Path(location.stdout.strip())
    assert user_site.is_relative_to(userbase)
    user_site.mkdir(parents=True)
    (user_site / "agy_doctor_user_site_fixture.py").write_text(f"""
from pathlib import Path
Path({str(marker)!r}).write_text("imported", encoding="utf-8")
""", encoding="utf-8")
    result = subprocess.run(
        [str(executable), "-B", *([] if flag == "normal" else [flag]), "-c", """
import json
import sys
from pathlib import Path
flag, marker = sys.argv[1:]
assert bool(sys.flags.no_user_site) == (flag == "-s")
assert bool(sys.flags.no_site) == (flag == "-S")
from agy_mcp import doctor
from agy_mcp.safety import SafetyPolicy
try:
    import agy_mcp.server
except ModuleNotFoundError as exc:
    assert exc.name == "agy_doctor_user_site_fixture"
    caller_failed = True
else:
    caller_failed = False
    assert agy_mcp.server._config is None
    assert agy_mcp.server._store is None
    assert agy_mcp.server._supervisor is None
assert caller_failed == (flag != "normal")
assert Path(marker).exists() == (flag == "normal")
print(json.dumps(doctor._check_mcp_server(SafetyPolicy()).to_dict()))
""", flag, str(marker)],
        capture_output=True, text=True, env=env, cwd=probe_environment, timeout=15,
    )
    assert result.returncode == 0 and result.stderr == ""
    check = json.loads(result.stdout)

    assert check["ok"] is (flag == "normal")
    assert marker.exists() is (flag == "normal")
    if flag != "normal":
        assert "ModuleNotFoundError: No module named 'agy_doctor_user_site_fixture'" in check["detail"]
    assert (probe_environment / "config.toml").read_text(encoding="utf-8") == ""
    assert not (probe_environment / "sessions").exists()
