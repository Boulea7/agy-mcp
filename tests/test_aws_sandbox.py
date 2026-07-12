"""Tests for the AWS sandbox provider."""

from __future__ import annotations

import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agy_mcp import aws_sandbox


@pytest.fixture(autouse=True)
def _scheduler_role(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "AGY_AWS_SCHEDULER_ROLE_ARN",
        "arn:aws:iam::123456789012:role/agy-sandbox-expiry",
    )


def _write_fake_aws(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import socket
import sys
import time

if any(os.environ.get(name) for name in (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)):
    print("static AWS credentials reached child", file=sys.stderr)
    raise SystemExit(90)

args = sys.argv[1:]
log_path = os.path.splitext(sys.argv[0])[0] + ".log"
data_path = os.path.splitext(sys.argv[0])[0] + ".data.json"
with open(log_path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
try:
    with open(data_path, encoding="utf-8") as handle:
        data = json.load(handle)
except FileNotFoundError:
    data = {}

def save_data():
    with open(data_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)

if "sts" in args and "get-caller-identity" in args:
    print(json.dumps({"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/test"}))
elif "scheduler" in args and "create-schedule" in args:
    if data.get("fail_schedule_create"):
        print("scheduler create failed", file=sys.stderr)
        raise SystemExit(42)
    data["schedule_target"] = json.loads(args[args.index("--target") + 1])
    data["schedule_expression"] = args[args.index("--schedule-expression") + 1]
    save_data()
    print(json.dumps({
        "ScheduleArn": "arn:aws:scheduler:"
        + args[args.index("--region") + 1]
        + ":123456789012:schedule/default/"
        + args[args.index("--name") + 1],
    }))
elif "scheduler" in args and "delete-schedule" in args:
    if data.get("fail_schedule_delete"):
        print("scheduler delete failed", file=sys.stderr)
        raise SystemExit(43)
    data.pop("schedule_target", None)
    save_data()
    print("{}")
elif "devicefarm" in args and "create-remote-access-session" in args:
    print(json.dumps({
        "remoteAccessSession": {
            "arn": "arn:aws:devicefarm:us-west-2:123456789012:session:project/abc",
            "status": "PENDING",
            "endpoints": {
                "remoteDriverEndpoint": "https://example.invalid/driver?X-Amz-Signature=fake",
                "interactiveEndpoint": "https://example.invalid/ui?X-Amz-Signature=fake",
            },
        }
    }))
elif "devicefarm" in args and "tag-resource" in args:
    data["devicefarm_tags"] = json.loads(args[args.index("--tags") + 1])
    save_data()
    print("{}")
elif "devicefarm" in args and "list-tags-for-resource" in args:
    print(json.dumps({"Tags": data.get("devicefarm_tags", [])}))
elif "devicefarm" in args and "get-remote-access-session" in args:
    print(json.dumps({
        "remoteAccessSession": {
            "arn": "arn:aws:devicefarm:us-west-2:123456789012:session:project/abc",
            "status": "RUNNING",
            "message": "device is ready",
            "endpoints": {
                "remoteDriverEndpoint": data.get(
                    "remote_driver_endpoint",
                    "https://example.invalid/driver?X-Amz-Signature=fake",
                ),
            },
        }
    }))
elif "devicefarm" in args and "stop-remote-access-session" in args:
    print(json.dumps({"remoteAccessSession": {"status": "STOPPING"}}))
elif "ec2" in args and "run-instances" in args:
    specifications = json.loads(args[args.index("--tag-specifications") + 1])
    data["ec2_tags"] = specifications[0]["Tags"]
    data["ec2_client_token"] = args[args.index("--client-token") + 1]
    save_data()
    if data.get("fail_run_instances"):
        print("EC2 run-instances result is ambiguous", file=sys.stderr)
        raise SystemExit(44)
    print(json.dumps({
        "Instances": [{
            "InstanceId": "i-0123456789abcdef0",
            "State": {"Name": "pending"},
        }]
    }))
elif "ec2" in args and "describe-instances" in args:
    if "--filters" in args and "ec2_client_token" not in data:
        print(json.dumps({"Reservations": []}))
        raise SystemExit(0)
    print(json.dumps({
        "Reservations": [{
            "Instances": [{
                "InstanceId": "i-0123456789abcdef0",
                "ClientToken": data.get("ec2_client_token", ""),
                "State": {"Name": "running"},
                "Tags": data.get("ec2_tags", []),
            }]
        }]
    }))
elif "ec2" in args and "terminate-instances" in args:
    print(json.dumps({
        "TerminatingInstances": [{
            "InstanceId": "i-0123456789abcdef0",
            "CurrentState": {"Name": "shutting-down"},
        }]
    }))
elif "ec2" in args and "get-console-output" in args:
    print(json.dumps({
        "Output": "booting\\nservice ready\\nGITHUB_TOKEN=ghp_" + "a" * 40,
    }))
elif "ssm" in args and "start-session" in args:
    parameters = json.loads(args[args.index("--parameters") + 1])
    port = int(parameters["localPortNumber"][0])
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen()
    while True:
        connection, _ = server.accept()
        connection.close()
else:
    print(json.dumps({"unexpected": args}), file=sys.stderr)
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_json(capsys: pytest.CaptureFixture[str], *args: str) -> dict:
    code = aws_sandbox.main([*args, "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out)


def test_android_start_creates_tagged_session_without_exposing_signed_endpoints(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    state_root = tmp_path / "state"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "us-west-2")
    monkeypatch.setenv(
        "AGY_AWS_DEVICEFARM_PROJECT_ARN",
        "arn:aws:devicefarm:us-west-2:123456789012:project:project",
    )
    monkeypatch.setenv(
        "AGY_AWS_DEVICEFARM_DEVICE_ARN",
        "arn:aws:devicefarm:us-west-2::device:device",
    )
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(state_root))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE000000000000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-real-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "not-a-real-session-token")

    result = _run_json(capsys, "start", "--target", "android-real")

    assert result["provider"] == "aws"
    assert result["target"] == "android-real"
    assert result["status"] == "pending"
    assert result["sandbox_id"].startswith("aws-android-")
    rendered = json.dumps(result)
    assert "remoteDriverEndpoint" not in rendered
    assert "interactiveEndpoint" not in rendered
    assert "X-Amz-" not in rendered

    state = json.loads((state_root / result["sandbox_id"] / "state.json").read_text())
    state_rendered = json.dumps(state)
    assert "X-Amz-" not in state_rendered
    assert state["resource_type"] == "devicefarm"
    assert state["account_id"] == "123456789012"

    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    assert any("create-remote-access-session" in call for call in calls)
    tag_call = next(call for call in calls if "tag-resource" in call)
    tags = json.loads(tag_call[tag_call.index("--tags") + 1])
    assert isinstance(tags, list)
    tag_map = {tag["Key"]: tag["Value"] for tag in tags}
    assert tag_map["agy:sandbox-id"] == result["sandbox_id"]
    assert tag_map["agy:expires-at"] == str(state["expires_at"])
    schedule_call = next(call for call in calls if "create-schedule" in call)
    target = json.loads(schedule_call[schedule_call.index("--target") + 1])
    assert target["Arn"] == (
        "arn:aws:scheduler:::aws-sdk:devicefarm:stopRemoteAccessSession"
    )
    assert json.loads(target["Input"]) == {"arn": state["resource_id"]}
    assert "at(" in schedule_call[schedule_call.index("--schedule-expression") + 1]
    assert "DELETE" in schedule_call


def test_aws_environment_preserves_safe_container_role_references(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE000000000000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-real-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "not-a-real-session-token")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/credentials/test")
    monkeypatch.setenv(
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "http://169.254.170.2/v2/credentials/test",
    )
    monkeypatch.setenv(
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "/var/run/secrets/ecs/token",
    )

    environment = aws_sandbox._aws_environment()

    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "AWS_SESSION_TOKEN" not in environment
    assert environment["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"] == "/v2/credentials/test"
    assert environment["AWS_CONTAINER_CREDENTIALS_FULL_URI"].startswith(
        "http://169.254.170.2/"
    )
    assert environment["AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"] == (
        "/var/run/secrets/ecs/token"
    )


def test_aws_environment_rejects_external_container_credentials_uri(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "https://credentials.example.com/session",
    )

    with pytest.raises(
        aws_sandbox.AwsSandboxError,
        match="AWS_CONTAINER_CREDENTIALS_FULL_URI is not a permitted container endpoint",
    ):
        aws_sandbox._aws_environment()


def test_aws_environment_rejects_invalid_relative_container_credentials_uri(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "http://169.254.170.2/v2/credentials/test",
    )

    with pytest.raises(
        aws_sandbox.AwsSandboxError,
        match="AWS_CONTAINER_CREDENTIALS_RELATIVE_URI must be an absolute path",
    ):
        aws_sandbox._aws_environment()


def test_aws_environment_only_passes_token_file_for_safe_full_uri(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "/var/run/secrets/ecs/token",
    )

    assert "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE" not in (
        aws_sandbox._aws_environment()
    )


def test_pc_vm_start_uses_launch_template_client_token_and_owner_tags(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    state_root = tmp_path / "state"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(state_root))

    result = _run_json(capsys, "start", "--target", "pc-vm")

    assert result["provider"] == "aws"
    assert result["target"] == "pc-vm"
    assert result["status"] == "pending"
    assert result["sandbox_id"].startswith("aws-pc-")
    assert result["metadata"]["resource_type"] == "ec2"
    assert result["metadata"]["resource_id"] == "i-0123456789abcdef0"
    assert result["metadata"]["expiry_enforcement"] == "eventbridge-scheduler"

    state = json.loads((state_root / result["sandbox_id"] / "state.json").read_text())
    assert state["resource_type"] == "ec2"
    assert state["resource_id"] == "i-0123456789abcdef0"
    assert state["region"] == "ap-southeast-1"

    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    run_call = next(call for call in calls if "run-instances" in call)
    assert "--client-token" in run_call
    assert run_call[run_call.index("--client-token") + 1] == result["sandbox_id"]
    launch_template = run_call[run_call.index("--launch-template") + 1]
    assert "LaunchTemplateId=lt-0123456789abcdef0" in launch_template
    tags = run_call[run_call.index("--tag-specifications") + 1]
    assert "agy:owner" in tags
    assert result["sandbox_id"] in tags
    assert "agy:expires-at" in tags
    schedule_call = next(call for call in calls if "create-schedule" in call)
    target = json.loads(schedule_call[schedule_call.index("--target") + 1])
    assert target["Arn"] == (
        "arn:aws:scheduler:::aws-sdk:ec2:terminateInstances"
    )
    assert json.loads(target["Input"]) == {
        "InstanceIds": ["i-0123456789abcdef0"]
    }


def test_pc_vm_start_fails_closed_and_terminates_when_expiry_schedule_fails(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    fake_data = fake_aws.with_suffix(".data.json")
    _write_fake_aws(fake_aws)
    fake_data.write_text(json.dumps({"fail_schedule_create": True}), encoding="utf-8")
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))

    code = aws_sandbox.main(["start", "--target", "pc-vm", "--json"])
    captured = capsys.readouterr()

    assert code == 1
    assert "scheduler create failed" in captured.err
    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    assert any("run-instances" in call for call in calls)
    assert any("create-schedule" in call for call in calls)
    assert any("terminate-instances" in call for call in calls)
    assert any("delete-schedule" in call for call in calls)


def test_pc_vm_start_reconciles_ambiguous_create_by_client_token_and_tags(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    fake_data = fake_aws.with_suffix(".data.json")
    _write_fake_aws(fake_aws)
    fake_data.write_text(json.dumps({"fail_run_instances": True}), encoding="utf-8")
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))

    result = _run_json(capsys, "start", "--target", "pc-vm")

    assert result["metadata"]["resource_id"] == "i-0123456789abcdef0"
    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    assert sum("run-instances" in call for call in calls) == 2
    reconcile = next(
        call
        for call in calls
        if "describe-instances" in call and "--filters" in call
    )
    filters = json.loads(reconcile[reconcile.index("--filters") + 1])
    names = {item["Name"] for item in filters}
    assert names == {
        "client-token",
        "tag:agy:owner",
        "tag:agy:sandbox-id",
        "tag:agy:expires-at",
    }
    assert any("create-schedule" in call for call in calls)


def test_pc_vm_start_requires_same_account_scheduler_role_before_launch(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AGY_AWS_SCHEDULER_ROLE_ARN")

    code = aws_sandbox.main(["start", "--target", "pc-vm", "--json"])
    captured = capsys.readouterr()

    assert code == 1
    assert "Scheduler execution role ARN is required" in captured.err
    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    assert not any("run-instances" in call for call in calls)


def test_android_status_refreshes_without_exposing_signed_endpoint(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    state_root = tmp_path / "state"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "us-west-2")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_PROJECT_ARN", "project-arn")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_DEVICE_ARN", "device-arn")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(state_root))
    started = _run_json(capsys, "start", "--target", "android-real")

    status = _run_json(capsys, "status", "--id", started["sandbox_id"])

    assert status["status"] == "running"
    assert status["endpoint"] is None
    assert "X-Amz-" not in json.dumps(status)
    state = json.loads((state_root / started["sandbox_id"] / "state.json").read_text())
    assert state["status"] == "running"
    assert "X-Amz-" not in json.dumps(state)


def test_pc_vm_status_refreshes_instance_state(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    state_root = tmp_path / "state"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(state_root))
    started = _run_json(capsys, "start", "--target", "pc-vm")

    status = _run_json(capsys, "status", "--id", started["sandbox_id"])

    assert status["status"] == "running"
    assert status["metadata"]["resource_id"] == "i-0123456789abcdef0"


def test_android_stop_verifies_tags_and_stops_remote_session(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "us-west-2")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_PROJECT_ARN", "project-arn")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_DEVICE_ARN", "device-arn")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    started = _run_json(capsys, "start", "--target", "android-real")

    stopped = _run_json(capsys, "stop", "--id", started["sandbox_id"])

    assert stopped["status"] == "stopping"
    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    assert any("list-tags-for-resource" in call for call in calls)
    assert any("stop-remote-access-session" in call for call in calls)
    assert any("delete-schedule" in call for call in calls)


def test_pc_vm_stop_verifies_tags_and_terminates_instance(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    started = _run_json(capsys, "start", "--target", "pc-vm")

    stopped = _run_json(capsys, "stop", "--id", started["sandbox_id"])

    assert stopped["status"] == "stopping"
    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    assert any("describe-instances" in call for call in calls)
    assert any("terminate-instances" in call for call in calls)
    assert any("delete-schedule" in call for call in calls)


def test_pc_vm_stop_reports_expiry_schedule_cleanup_failure_after_termination(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_data = fake_aws.with_suffix(".data.json")
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    started = _run_json(capsys, "start", "--target", "pc-vm")
    data = json.loads(fake_data.read_text())
    data["fail_schedule_delete"] = True
    fake_data.write_text(json.dumps(data), encoding="utf-8")

    stopped = _run_json(capsys, "stop", "--id", started["sandbox_id"])

    assert stopped["status"] == "stopping"
    assert stopped["warnings"] == [
        "expiry schedule cleanup failed: AWS CLI failed: scheduler delete failed"
    ]
    data = json.loads(fake_data.read_text())
    data.pop("fail_schedule_delete")
    fake_data.write_text(json.dumps(data), encoding="utf-8")

    retried = _run_json(capsys, "stop", "--id", started["sandbox_id"])

    assert "warnings" not in retried


def test_pc_vm_stop_refuses_remote_owner_tag_mismatch(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    fake_data = fake_aws.with_suffix(".data.json")
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    started = _run_json(capsys, "start", "--target", "pc-vm")
    data = json.loads(fake_data.read_text())
    for tag in data["ec2_tags"]:
        if tag["Key"] == "agy:owner":
            tag["Value"] = "0" * 32
    fake_data.write_text(json.dumps(data), encoding="utf-8")

    code = aws_sandbox.main(["stop", "--id", started["sandbox_id"], "--json"])
    captured = capsys.readouterr()

    assert code == 1
    assert "ownership" in captured.err
    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    assert not any("terminate-instances" in call for call in calls)


def test_pc_vm_logs_verify_ownership_tail_and_redact_console_output(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    started = _run_json(capsys, "start", "--target", "pc-vm")

    result = _run_json(capsys, "logs", "--id", started["sandbox_id"], "--tail", "2")

    assert "booting" not in result["logs"]
    assert "service ready" in result["logs"]
    assert "ghp_" not in result["logs"]
    assert "***" in result["logs"]
    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    describe_index = next(index for index, call in enumerate(calls) if "describe-instances" in call)
    logs_index = next(index for index, call in enumerate(calls) if "get-console-output" in call)
    assert describe_index < logs_index


def test_android_logs_return_session_message_without_endpoint(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "us-west-2")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_PROJECT_ARN", "project-arn")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_DEVICE_ARN", "device-arn")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    started = _run_json(capsys, "start", "--target", "android-real")

    result = _run_json(capsys, "logs", "--id", started["sandbox_id"], "--tail", "20")

    assert result["logs"] == "device is ready"
    assert "X-Amz-" not in json.dumps(result)


def test_android_attach_relays_remote_driver_without_persisting_signed_url(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    received_paths: list[str] = []

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            received_paths.append(self.path)
            body = (
                b'{"value":"ready","signed":"https://example.invalid/'
                b'?X-Amz-Signature=fake"}'
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Location", "https://example.invalid/?X-Amz-Signature=fake")
            self.send_header("Set-Cookie", "session=fake")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    fake_aws = tmp_path / "aws"
    fake_data = fake_aws.with_suffix(".data.json")
    state_root = tmp_path / "state"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "us-west-2")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_PROJECT_ARN", "project-arn")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_DEVICE_ARN", "device-arn")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(state_root))
    started = _run_json(capsys, "start", "--target", "android-real")
    data = json.loads(fake_data.read_text())
    data["remote_driver_endpoint"] = (
        f"http://127.0.0.1:{upstream.server_port}/wd/hub?X-Amz-Signature=fake"
    )
    fake_data.write_text(json.dumps(data), encoding="utf-8")
    stopped = False

    try:
        attached = _run_json(capsys, "attach", "--id", started["sandbox_id"])
        assert attached["endpoint"].startswith("http://127.0.0.1:")
        assert "X-Amz-" not in json.dumps(attached)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{attached['endpoint']}/status", timeout=5) as response:
            body = response.read()
            assert json.loads(body)["value"] == "ready"
            assert b"X-Amz-Signature=fake" not in body
            assert response.headers.get("Location") is None
            assert response.headers.get("Set-Cookie") is None
        assert received_paths == ["/wd/hub/status?X-Amz-Signature=fake"]
        state = json.loads((state_root / started["sandbox_id"] / "state.json").read_text())
        assert "X-Amz-" not in json.dumps(state)

        _run_json(capsys, "stop", "--id", started["sandbox_id"])
        stopped = True
        port = int(attached["endpoint"].rsplit(":", 1)[1])
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)
    finally:
        if not stopped:
            aws_sandbox.main(["stop", "--id", started["sandbox_id"], "--json"])
            capsys.readouterr()
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)


def test_pc_vm_attach_starts_silent_ssm_rdp_tunnel_and_stop_cleans_it(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    state_root = tmp_path / "state"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(state_root))
    started = _run_json(capsys, "start", "--target", "pc-vm")
    stopped = False

    try:
        attached = _run_json(capsys, "attach", "--id", started["sandbox_id"])
        assert attached["endpoint"].startswith("rdp://127.0.0.1:")
        port = int(attached["endpoint"].rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        state = json.loads((state_root / started["sandbox_id"] / "state.json").read_text())
        rendered = json.dumps(state)
        assert "sessionId" not in rendered
        assert "StreamUrl" not in rendered
        assert "TokenValue" not in rendered
        calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
        ssm_call = next(call for call in calls if "start-session" in call)
        assert "AWS-StartPortForwardingSession" in ssm_call
        parameters = json.loads(ssm_call[ssm_call.index("--parameters") + 1])
        assert parameters["portNumber"] == ["3389"]
        assert parameters["localPortNumber"] == [str(port)]

        _run_json(capsys, "stop", "--id", started["sandbox_id"])
        stopped = True
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)
    finally:
        if not stopped:
            aws_sandbox.main(["stop", "--id", started["sandbox_id"], "--json"])
            capsys.readouterr()


def test_android_start_cleans_remote_session_when_state_write_fails(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "us-west-2")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_PROJECT_ARN", "project-arn")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_DEVICE_ARN", "device-arn")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))

    def fail_write(_state):
        raise aws_sandbox.AwsSandboxError("state write failed")

    monkeypatch.setattr(aws_sandbox, "_write_state", fail_write)

    code = aws_sandbox.main(["start", "--target", "android-real", "--json"])
    capsys.readouterr()

    assert code == 1
    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    assert any("stop-remote-access-session" in call for call in calls)


def test_pc_vm_start_terminates_instance_when_state_write_fails(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))

    def fail_write(_state):
        raise aws_sandbox.AwsSandboxError("state write failed")

    monkeypatch.setattr(aws_sandbox, "_write_state", fail_write)

    code = aws_sandbox.main(["start", "--target", "pc-vm", "--json"])
    capsys.readouterr()

    assert code == 1
    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    assert any("terminate-instances" in call for call in calls)


def test_android_start_reports_resource_when_compensation_cleanup_fails(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "us-west-2")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_PROJECT_ARN", "project-arn")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_DEVICE_ARN", "device-arn")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        aws_sandbox,
        "_write_state",
        lambda _state: (_ for _ in ()).throw(
            aws_sandbox.AwsSandboxError("state write failed")
        ),
    )
    monkeypatch.setattr(
        aws_sandbox,
        "_stop_new_devicefarm_session",
        lambda *_args, **_kwargs: "cleanup unavailable",
    )

    code = aws_sandbox.main(["start", "--target", "android-real", "--json"])
    captured = capsys.readouterr()

    assert code == 1
    assert "state write failed" in captured.err
    assert "cleanup failed" in captured.err
    assert "arn:aws:devicefarm:" in captured.err


def test_pc_vm_start_reports_resource_when_compensation_cleanup_fails(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        aws_sandbox,
        "_write_state",
        lambda _state: (_ for _ in ()).throw(
            aws_sandbox.AwsSandboxError("state write failed")
        ),
    )
    monkeypatch.setattr(
        aws_sandbox,
        "_terminate_new_ec2_instance",
        lambda *_args, **_kwargs: "cleanup unavailable",
    )

    code = aws_sandbox.main(["start", "--target", "pc-vm", "--json"])
    captured = capsys.readouterr()

    assert code == 1
    assert "state write failed" in captured.err
    assert "cleanup failed" in captured.err
    assert "i-0123456789abcdef0" in captured.err


def test_android_attach_stops_relay_when_state_write_fails(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "us-west-2")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_PROJECT_ARN", "project-arn")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_DEVICE_ARN", "device-arn")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    started = _run_json(capsys, "start", "--target", "android-real")
    relay = {"pid": 123, "pgid": 123, "start_token": "proc:1", "port": 4567}
    stopped: list[dict] = []
    monkeypatch.setattr(aws_sandbox, "_start_http_relay", lambda _upstream: relay)
    monkeypatch.setattr(aws_sandbox, "_terminate_process", lambda process: stopped.append(process) or True)
    monkeypatch.setattr(
        aws_sandbox,
        "_write_state",
        lambda _state: (_ for _ in ()).throw(aws_sandbox.AwsSandboxError("state write failed")),
    )

    code = aws_sandbox.main(["attach", "--id", started["sandbox_id"], "--json"])
    capsys.readouterr()

    assert code == 1
    assert stopped == [relay]


def test_pc_vm_attach_stops_ssm_process_when_state_write_fails(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    started = _run_json(capsys, "start", "--target", "pc-vm")
    process = {"pid": 123, "pgid": 123, "start_token": "proc:1"}
    stopped: list[dict] = []
    monkeypatch.setattr(aws_sandbox, "_spawn_silent_detached", lambda _command: process)
    monkeypatch.setattr(aws_sandbox, "_wait_for_loopback_port", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(aws_sandbox, "_terminate_process", lambda value: stopped.append(value) or True)
    monkeypatch.setattr(
        aws_sandbox,
        "_write_state",
        lambda _state: (_ for _ in ()).throw(aws_sandbox.AwsSandboxError("state write failed")),
    )

    code = aws_sandbox.main(["attach", "--id", started["sandbox_id"], "--json"])
    capsys.readouterr()

    assert code == 1
    assert len(stopped) == 1
    assert stopped[0]["pid"] == 123
    assert stopped[0]["kind"] == "ssm-rdp"
    assert stopped[0]["host"] == "127.0.0.1"


def test_gc_previews_expired_sandbox_and_requires_execute_to_terminate(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    fake_log = fake_aws.with_suffix(".log")
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(aws_sandbox.time, "time", lambda: 100.0)
    started = _run_json(capsys, "start", "--target", "pc-vm", "--ttl", "600")
    monkeypatch.setattr(aws_sandbox.time, "time", lambda: 701.0)

    preview = _run_json(capsys, "gc")

    assert preview["expired"] == [started["sandbox_id"]]
    assert preview["stopped"] == []
    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    assert not any("terminate-instances" in call for call in calls)

    executed = _run_json(capsys, "gc", "--execute")

    assert executed["expired"] == [started["sandbox_id"]]
    assert executed["stopped"] == [started["sandbox_id"]]
    calls = [json.loads(line) for line in fake_log.read_text().splitlines()]
    assert any("terminate-instances" in call for call in calls)


def test_sandbox_lock_serializes_control_actions(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def hold_lock():
        with aws_sandbox._sandbox_lock("aws-pc-test"):
            entered.set()
            assert release.wait(timeout=5)

    def wait_for_lock():
        assert entered.wait(timeout=5)
        with aws_sandbox._sandbox_lock("aws-pc-test"):
            second_entered.set()

    first = threading.Thread(target=hold_lock)
    second = threading.Thread(target=wait_for_lock)
    first.start()
    second.start()
    assert entered.wait(timeout=5)
    assert not second_entered.wait(timeout=0.2)
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()


def test_invalid_numeric_environment_returns_controlled_error(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_SANDBOX_TTL_SECONDS", "not-an-integer")

    code = aws_sandbox.main(["start", "--target", "pc-vm", "--json"])
    captured = capsys.readouterr()

    assert code == 1
    payload = json.loads(captured.err)
    assert payload["status"] == "error"
    assert "AGY_AWS_SANDBOX_TTL_SECONDS" in payload["error"]
    assert not fake_aws.with_suffix(".log").exists()


def test_status_does_not_trust_persisted_endpoint(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    state_root = tmp_path / "state"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "us-west-2")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_PROJECT_ARN", "project-arn")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_DEVICE_ARN", "device-arn")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(state_root))
    started = _run_json(capsys, "start", "--target", "android-real")
    state_path = state_root / started["sandbox_id"] / "state.json"
    state = json.loads(state_path.read_text())
    state["endpoint"] = "https://example.invalid/?X-Amz-Signature=fake"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    status = _run_json(capsys, "status", "--id", started["sandbox_id"])

    assert status["endpoint"] is None
    assert "X-Amz-" not in json.dumps(status)


def test_stopping_state_still_cleans_local_relay(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    state_root = tmp_path / "state"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", "lt-0123456789abcdef0")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(state_root))
    started = _run_json(capsys, "start", "--target", "pc-vm")
    state_path = state_root / started["sandbox_id"] / "state.json"
    state = json.loads(state_path.read_text())
    relay = {"pid": 123, "pgid": 123, "start_token": "proc:1", "port": 4567}
    state["status"] = "stopping"
    state["relay"] = relay
    state["endpoint"] = "rdp://127.0.0.1:4567"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    stopped: list[dict] = []
    monkeypatch.setattr(aws_sandbox, "_terminate_process", lambda process: stopped.append(process) or True)

    result = _run_json(capsys, "stop", "--id", started["sandbox_id"])

    assert result["status"] == "stopping"
    assert result["endpoint"] is None
    assert stopped == [relay]
    persisted = json.loads(state_path.read_text())
    assert "relay" not in persisted
    assert "endpoint" not in persisted


def test_process_identity_rejects_live_process_group_mismatch(
    monkeypatch: pytest.MonkeyPatch,
):
    process = {"pid": 123, "pgid": 999, "start_token": "proc:1"}
    monkeypatch.setattr(aws_sandbox, "_pid_running", lambda _pid: True)
    monkeypatch.setattr(aws_sandbox, "_process_start_token", lambda _pid: "proc:1")
    monkeypatch.setattr(aws_sandbox.os, "getpgid", lambda _pid: 321)

    assert aws_sandbox._process_running(process) is False


def test_process_start_token_falls_back_to_ps(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(aws_sandbox.Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(
        aws_sandbox.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="Mon Jul 13 12:34:56 2026\n",
            stderr="",
        ),
    )

    assert aws_sandbox._process_start_token(123) == "ps:Mon Jul 13 12:34:56 2026"


def test_android_start_rejects_ttl_above_device_farm_limit(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))
    monkeypatch.setenv("AGY_AWS_REGION", "us-west-2")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_PROJECT_ARN", "project-arn")
    monkeypatch.setenv("AGY_AWS_DEVICEFARM_DEVICE_ARN", "device-arn")
    monkeypatch.setenv("AGY_AWS_SANDBOX_STATE_DIR", str(tmp_path / "state"))

    code = aws_sandbox.main(
        ["start", "--target", "android-real", "--ttl", "9001", "--json"]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert "9000" in captured.err
    assert not fake_aws.with_suffix(".log").exists()


def test_start_rejects_ttl_below_scheduler_lead_time(
    isolated_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_aws = tmp_path / "aws"
    _write_fake_aws(fake_aws)
    monkeypatch.setenv("AGY_AWS_CLI", str(fake_aws))

    code = aws_sandbox.main(
        ["start", "--target", "pc-vm", "--ttl", "599", "--json"]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert "600" in captured.err
    assert not fake_aws.with_suffix(".log").exists()
