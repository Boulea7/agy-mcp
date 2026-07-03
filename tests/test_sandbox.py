"""Tests for provider-neutral sandbox launch hooks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agy_mcp.config import Config, SandboxProviderConfig
from agy_mcp.safety import SafetyPolicy
from agy_mcp.sandbox import (
    sandbox_attach,
    sandbox_logs,
    sandbox_status,
    start_sandbox,
    stop_sandbox,
)


def _write_launcher(tmp_path: Path, body: str) -> Path:
    launcher = tmp_path / "sandbox_launcher.py"
    launcher.write_text(
        f"#!{sys.executable}\n{body}",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


def _config_with_provider(launcher: Path, *, start: list[str] | None = None) -> Config:
    config = Config()
    config.sandbox.default_provider = "fake"
    config.sandbox.default_timeout = 5
    config.sandbox.providers["fake"] = SandboxProviderConfig(
        start=start
        or [
            str(launcher),
            "--target",
            "{target}",
            "--scenario",
            "{scenario}",
            "--cwd",
            "{cwd}",
            "--provider",
            "{provider}",
        ],
        status=[
            str(launcher),
            "--action",
            "status",
            "--id",
            "{sandbox_id}",
            "--provider",
            "{provider}",
        ],
        stop=[
            str(launcher),
            "--action",
            "stop",
            "--id",
            "{sandbox_id}",
        ],
        logs=[
            str(launcher),
            "--action",
            "logs",
            "--id",
            "{sandbox_id}",
            "--tail",
            "{tail}",
        ],
        attach=[
            str(launcher),
            "--action",
            "attach",
            "--id",
            "{sandbox_id}",
        ],
    )
    return config


def test_start_sandbox_dry_run_returns_command_preview(tmp_path: Path):
    launcher = _write_launcher(tmp_path, "raise SystemExit(0)\n")
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = start_sandbox(
        config=config,
        safety=safety,
        provider=None,
        target="mobile",
        cwd=str(tmp_path),
        scenario="login-smoke",
        dry_run=True,
    )

    assert out.success is True
    assert out.provider == "fake"
    assert out.target == "mobile"
    assert out.status == "dry_run"
    assert out.command_preview is not None
    assert "--target" in out.command_preview
    assert "mobile" in out.command_preview


def test_start_sandbox_executes_provider_and_maps_json_fields(tmp_path: Path):
    launcher = _write_launcher(
        tmp_path,
        """
import json
import sys

print(json.dumps({
    "sandbox_id": "box-123",
    "status": "running",
    "endpoint": "http://127.0.0.1:5900",
    "argv": sys.argv[1:],
}))
""".lstrip(),
    )
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = start_sandbox(
        config=config,
        safety=safety,
        provider="fake",
        target="pc",
        cwd=str(tmp_path),
        scenario="smoke",
    )

    assert out.success is True
    assert out.status == "running"
    assert out.sandbox_id == "box-123"
    assert out.endpoint == "http://127.0.0.1:5900"
    assert out.metadata["argv"] == [
        "--target",
        "pc",
        "--scenario",
        "smoke",
        "--cwd",
        str(tmp_path),
        "--provider",
        "fake",
    ]


def test_start_sandbox_coerces_scalar_provider_fields(tmp_path: Path):
    launcher = _write_launcher(
        tmp_path,
        """
import json

print(json.dumps({
    "sandbox_id": 12345,
    "status": 7,
    "endpoint": 5900,
}))
""".lstrip(),
    )
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = start_sandbox(
        config=config,
        safety=safety,
        provider="fake",
        target="pc",
        cwd=str(tmp_path),
    )

    assert out.sandbox_id == "12345"
    assert out.status == "7"
    assert out.endpoint == "5900"


def test_start_sandbox_accepts_json_on_last_stdout_line(tmp_path: Path):
    launcher = _write_launcher(
        tmp_path,
        """
import json

print("starting provider")
print(json.dumps({"sandbox_id": "box-last-line", "endpoint": "http://127.0.0.1:5900"}))
""".lstrip(),
    )
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = start_sandbox(
        config=config,
        safety=safety,
        provider="fake",
        target="pc",
        cwd=str(tmp_path),
    )

    assert out.success is True
    assert out.sandbox_id == "box-last-line"
    assert out.endpoint == "http://127.0.0.1:5900"


def test_start_sandbox_keeps_tail_json_after_large_stdout(tmp_path: Path):
    launcher = _write_launcher(
        tmp_path,
        """
import json
import sys

sys.stdout.write("x" * (300 * 1024))
print()
print(json.dumps({"sandbox_id": "box-tail", "status": "running"}))
""".lstrip(),
    )
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = start_sandbox(
        config=config,
        safety=safety,
        provider="fake",
        target="pc",
        cwd=str(tmp_path),
    )

    assert out.sandbox_id == "box-tail"


def test_sandbox_status_runs_provider_action(tmp_path: Path):
    launcher = _write_launcher(
        tmp_path,
        """
import json
import sys

print(json.dumps({"status": "ready", "argv": sys.argv[1:]}))
""".lstrip(),
    )
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = sandbox_status(
        config=config,
        safety=safety,
        provider="fake",
        sandbox_id="box-123",
        cwd=str(tmp_path),
    )

    assert out.success is True
    assert out.action == "status"
    assert out.status == "ready"
    assert out.sandbox_id == "box-123"
    assert out.metadata["argv"] == [
        "--action",
        "status",
        "--id",
        "box-123",
        "--provider",
        "fake",
    ]


def test_stop_sandbox_defaults_status_when_provider_omits_it(tmp_path: Path):
    launcher = _write_launcher(tmp_path, "print('{}')\n")
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = stop_sandbox(
        config=config,
        safety=safety,
        provider="fake",
        sandbox_id="box-123",
        cwd=str(tmp_path),
    )

    assert out.success is True
    assert out.action == "stop"
    assert out.status == "stopped"


def test_sandbox_logs_maps_text_and_tail(tmp_path: Path):
    launcher = _write_launcher(
        tmp_path,
        """
import json
import sys

print(json.dumps({"logs": "line1\\nline2", "argv": sys.argv[1:]}))
""".lstrip(),
    )
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = sandbox_logs(
        config=config,
        safety=safety,
        provider="fake",
        sandbox_id="box-123",
        cwd=str(tmp_path),
        tail=50,
    )

    assert out.success is True
    assert out.action == "logs"
    assert out.logs == "line1\nline2"
    assert out.metadata["argv"] == [
        "--action",
        "logs",
        "--id",
        "box-123",
        "--tail",
        "50",
    ]


def test_sandbox_attach_maps_endpoint(tmp_path: Path):
    launcher = _write_launcher(
        tmp_path,
        """
import json

print(json.dumps({"endpoint": "vnc://127.0.0.1:5900", "status": "attached"}))
""".lstrip(),
    )
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = sandbox_attach(
        config=config,
        safety=safety,
        provider="fake",
        sandbox_id="box-123",
        cwd=str(tmp_path),
    )

    assert out.success is True
    assert out.action == "attach"
    assert out.endpoint == "vnc://127.0.0.1:5900"
    assert out.status == "attached"


def test_sandbox_control_dry_run_returns_preview(tmp_path: Path):
    launcher = _write_launcher(tmp_path, "raise SystemExit(0)\n")
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = sandbox_status(
        config=config,
        safety=safety,
        provider="fake",
        sandbox_id="box-123",
        cwd=str(tmp_path),
        dry_run=True,
    )

    assert out.success is True
    assert out.status == "dry_run"
    assert out.command_preview is not None
    assert "box-123" in out.command_preview


def test_sandbox_control_rejects_bad_sandbox_id(tmp_path: Path):
    launcher = _write_launcher(tmp_path, "raise SystemExit(0)\n")
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    with pytest.raises(ValueError, match="sandbox_id must match"):
        sandbox_status(
            config=config,
            safety=safety,
            provider="fake",
            sandbox_id="../box",
            cwd=str(tmp_path),
        )


def test_sandbox_control_rejects_dot_sandbox_id(tmp_path: Path):
    launcher = _write_launcher(tmp_path, "raise SystemExit(0)\n")
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    with pytest.raises(ValueError, match="sandbox_id must not"):
        sandbox_status(
            config=config,
            safety=safety,
            provider="fake",
            sandbox_id="..",
            cwd=str(tmp_path),
        )


def test_sandbox_logs_rejects_excessive_tail(tmp_path: Path):
    launcher = _write_launcher(tmp_path, "raise SystemExit(0)\n")
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    with pytest.raises(ValueError, match="tail exceeds"):
        sandbox_logs(
            config=config,
            safety=safety,
            provider="fake",
            sandbox_id="box-123",
            cwd=str(tmp_path),
            tail=10_001,
        )


def test_sandbox_control_reports_missing_provider_command(tmp_path: Path):
    launcher = _write_launcher(tmp_path, "raise SystemExit(0)\n")
    config = Config()
    config.sandbox.default_provider = "fake"
    config.sandbox.providers["fake"] = SandboxProviderConfig(start=[str(launcher)])
    safety = SafetyPolicy.from_config(config)

    with pytest.raises(ValueError, match="has no status command"):
        sandbox_status(
            config=config,
            safety=safety,
            provider="fake",
            sandbox_id="box-123",
            cwd=str(tmp_path),
        )


def test_start_sandbox_rejects_unknown_provider(tmp_path: Path):
    config = Config()
    safety = SafetyPolicy.from_config(config)

    with pytest.raises(ValueError, match="provider is required"):
        start_sandbox(
            config=config,
            safety=safety,
            provider=None,
            target="pc",
            cwd=str(tmp_path),
        )


def test_start_sandbox_rejects_bad_target_slug(tmp_path: Path):
    launcher = _write_launcher(tmp_path, "raise SystemExit(0)\n")
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    with pytest.raises(ValueError, match="target must match"):
        start_sandbox(
            config=config,
            safety=safety,
            provider="fake",
            target="../pc",
            cwd=str(tmp_path),
        )


def test_start_sandbox_rejects_dot_target(tmp_path: Path):
    launcher = _write_launcher(tmp_path, "raise SystemExit(0)\n")
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    with pytest.raises(ValueError, match="target must not"):
        start_sandbox(
            config=config,
            safety=safety,
            provider="fake",
            target=".",
            cwd=str(tmp_path),
        )


def test_start_sandbox_rejects_missing_executable(tmp_path: Path):
    config = _config_with_provider(tmp_path / "missing-launcher")
    safety = SafetyPolicy.from_config(config)

    with pytest.raises(ValueError, match="command not found"):
        start_sandbox(
            config=config,
            safety=safety,
            provider="fake",
            target="pc",
            cwd=str(tmp_path),
            dry_run=True,
        )


def test_start_sandbox_redacts_non_json_stdout(tmp_path: Path):
    launcher = _write_launcher(
        tmp_path,
        "print('Authorization: Bearer abcdef1234567890abcdef1234567890')\n",
    )
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = start_sandbox(
        config=config,
        safety=safety,
        provider="fake",
        target="pc",
        cwd=str(tmp_path),
    )

    assert out.success is True
    assert "abcdef1234567890abcdef1234567890" not in out.metadata["stdout"]
    assert "***" in out.metadata["stdout"]


def test_start_sandbox_scrubs_child_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 32)
    launcher = _write_launcher(
        tmp_path,
        """
import json
import os

print(json.dumps({"token": os.environ.get("GITHUB_TOKEN")}))
""".lstrip(),
    )
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = start_sandbox(
        config=config,
        safety=safety,
        provider="fake",
        target="pc",
        cwd=str(tmp_path),
    )

    assert out.success is True
    assert out.metadata["token"] == "***"


def test_start_sandbox_redacts_json_keys_and_values(tmp_path: Path):
    launcher = _write_launcher(
        tmp_path,
        """
import json

print(json.dumps({
    "Authorization: Bearer abcdef1234567890abcdef1234567890": "ok",
    "nested": {"token": "Authorization: Bearer abcdef1234567890abcdef1234567890"},
}))
""".lstrip(),
    )
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    out = start_sandbox(
        config=config,
        safety=safety,
        provider="fake",
        target="pc",
        cwd=str(tmp_path),
    )

    payload = out.model_dump_json()
    assert "abcdef1234567890abcdef1234567890" not in payload
    assert "Authorization: *** ***" in out.metadata
    assert out.metadata["nested"]["token"] == "Authorization: *** ***"


def test_start_sandbox_limits_stderr_failure_detail(tmp_path: Path):
    launcher = _write_launcher(
        tmp_path,
        """
import sys

sys.stderr.write("x" * (70 * 1024))
raise SystemExit(7)
""".lstrip(),
    )
    config = _config_with_provider(launcher)
    safety = SafetyPolicy.from_config(config)

    with pytest.raises(RuntimeError) as excinfo:
        start_sandbox(
            config=config,
            safety=safety,
            provider="fake",
            target="pc",
            cwd=str(tmp_path),
        )

    assert "sandbox provider exited 7" in str(excinfo.value)
    assert len(str(excinfo.value)) < 66 * 1024
