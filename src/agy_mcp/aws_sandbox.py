"""AWS sandbox provider for Device Farm devices and EC2 desktop VMs."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from agy_mcp.utils import redact_text

_ANDROID_TARGETS = {"android", "android-real", "mobile", "real-device"}
_PC_VM_TARGETS = {"desktop", "desktop-vm", "pc", "pc-vm", "vm", "windows"}
_SANDBOX_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ACCOUNT_ID_RE = re.compile(r"^[0-9]{12}$")
_SCHEDULER_ROLE_ARN_RE = re.compile(
    r"^arn:(aws(?:-[a-z]+)?):iam::([0-9]{12}):role/[\w+=,.@/-]+$"
)
_DEFAULT_REGION = "us-west-2"
_DEFAULT_TTL_SECONDS = 3600
_MIN_TTL_SECONDS = 10 * 60
_DEVICE_FARM_MAX_TTL_SECONDS = 150 * 60
_MAX_TTL_SECONDS = 24 * 60 * 60
_AWS_TIMEOUT_SECONDS = 120
_RELAY_TIMEOUT_SECONDS = 120
_RELAY_MAX_BODY_BYTES = 64 * 1024 * 1024
_AWS_ENV_ALLOWLIST = {
    "AWS_CA_BUNDLE",
    "AWS_CONFIG_FILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_EC2_METADATA_DISABLED",
    "AWS_PROFILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
}
_SIGNED_QUERY_RE = re.compile(
    r"(?i)(X-Amz-(?:Signature|Credential|Security-Token|Algorithm|Date|Expires)="
    r")[^&\s\"']+",
)


class AwsSandboxError(RuntimeError):
    """User-facing AWS provider failure."""


def main(argv: Sequence[str] | None = None) -> int:
    """Run the AWS sandbox provider CLI."""

    json_requested = argv is not None and "--json" in argv
    try:
        parser = _build_parser()
        args = parser.parse_args(argv)
        if args.command == "start":
            result = _cmd_start(args)
        elif args.command == "status":
            result = _locked_control(args.sandbox_id, _cmd_status, args)
        elif args.command == "stop":
            result = _locked_control(args.sandbox_id, _cmd_stop, args)
        elif args.command == "logs":
            result = _locked_control(args.sandbox_id, _cmd_logs, args)
        elif args.command == "attach":
            result = _locked_control(args.sandbox_id, _cmd_attach, args)
        elif args.command == "gc":
            result = _cmd_gc(args)
        else:
            raise AwsSandboxError(f"{args.command} is not implemented yet")
    except AwsSandboxError as exc:
        _emit_error(exc, json_output=json_requested)
        return 1
    _emit(result, json_output=args.json)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agy-aws-sandbox",
        description="Start and control AWS Device Farm devices or EC2 desktop VMs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start", help="start an AWS sandbox")
    start.add_argument("--target", required=True)
    start.add_argument("--scenario", default="")
    start.add_argument("--region", default=os.environ.get("AGY_AWS_REGION", _DEFAULT_REGION))
    start.add_argument("--profile", default=os.environ.get("AGY_AWS_PROFILE", ""))
    start.add_argument(
        "--project-arn",
        default=os.environ.get("AGY_AWS_DEVICEFARM_PROJECT_ARN", ""),
    )
    start.add_argument(
        "--device-arn",
        default=os.environ.get("AGY_AWS_DEVICEFARM_DEVICE_ARN", ""),
    )
    start.add_argument(
        "--interaction-mode",
        default=os.environ.get("AGY_AWS_DEVICEFARM_INTERACTION_MODE", "INTERACTIVE"),
        choices=("INTERACTIVE", "NO_VIDEO", "VIDEO_ONLY"),
    )
    start.add_argument(
        "--launch-template-id",
        default=os.environ.get("AGY_AWS_EC2_LAUNCH_TEMPLATE_ID", ""),
    )
    start.add_argument(
        "--launch-template-version",
        default=os.environ.get("AGY_AWS_EC2_LAUNCH_TEMPLATE_VERSION", "$Default"),
    )
    start.add_argument(
        "--scheduler-role-arn",
        default=os.environ.get("AGY_AWS_SCHEDULER_ROLE_ARN", ""),
    )
    start.add_argument(
        "--ttl",
        type=int,
        default=_env_int("AGY_AWS_SANDBOX_TTL_SECONDS", _DEFAULT_TTL_SECONDS),
    )
    start.add_argument("--json", action="store_true")

    for name in ("status", "stop"):
        command = subparsers.add_parser(name)
        command.add_argument("--id", required=True, dest="sandbox_id")
        command.add_argument("--json", action="store_true")
    attach = subparsers.add_parser("attach")
    attach.add_argument("--id", required=True, dest="sandbox_id")
    attach.add_argument(
        "--local-port",
        type=int,
        default=_env_int("AGY_AWS_SSM_LOCAL_PORT", 0),
    )
    attach.add_argument(
        "--rdp-port",
        type=int,
        default=_env_int("AGY_AWS_SSM_RDP_PORT", 3389),
    )
    attach.add_argument(
        "--startup-timeout",
        type=int,
        default=_env_int("AGY_AWS_ATTACH_TIMEOUT", 30),
    )
    attach.add_argument("--json", action="store_true")
    logs = subparsers.add_parser("logs")
    logs.add_argument("--id", required=True, dest="sandbox_id")
    logs.add_argument("--tail", type=int, default=200)
    logs.add_argument("--json", action="store_true")
    gc = subparsers.add_parser("gc", help="list or stop expired AWS sandboxes")
    gc.add_argument("--execute", action="store_true")
    gc.add_argument("--json", action="store_true")
    return parser


def _cmd_start(args: argparse.Namespace) -> dict[str, Any]:
    target = _canonical_target(args.target)
    if args.ttl < _MIN_TTL_SECONDS or args.ttl > _MAX_TTL_SECONDS:
        raise AwsSandboxError(
            f"ttl must be between {_MIN_TTL_SECONDS} and {_MAX_TTL_SECONDS} seconds"
        )
    if target == "android-real":
        if args.ttl > _DEVICE_FARM_MAX_TTL_SECONDS:
            raise AwsSandboxError(
                f"Device Farm ttl must not exceed {_DEVICE_FARM_MAX_TTL_SECONDS} seconds"
            )
        return _start_android(args)
    return _start_pc_vm(args)


def _cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    state = _load_state(args.sandbox_id)
    aws = _aws_cli()
    _verify_caller_account(aws, state)
    resource_type = state["resource_type"]
    if resource_type == "devicefarm":
        payload = _run_aws(
            aws,
            region=state["region"],
            profile=state["profile"],
            command=[
                "devicefarm",
                "get-remote-access-session",
                "--arn",
                state["resource_id"],
            ],
        )
        session = payload.get("remoteAccessSession")
        if not isinstance(session, dict):
            raise AwsSandboxError("Device Farm status response omitted remoteAccessSession")
        state["status"] = _normalize_status(session.get("status"), default="unknown")
    elif resource_type == "ec2":
        payload = _run_aws(
            aws,
            region=state["region"],
            profile=state["profile"],
            command=["ec2", "describe-instances", "--instance-ids", state["resource_id"]],
        )
        instance = _single_described_instance(payload)
        raw_state = instance.get("State")
        raw_status = raw_state.get("Name") if isinstance(raw_state, dict) else None
        state["status"] = _normalize_status(raw_status, default="unknown")
    else:
        raise AwsSandboxError("sandbox state has an unsupported resource type")
    state["updated_at"] = time.time()
    _write_state(state)
    return _response(state)


def _cmd_stop(args: argparse.Namespace) -> dict[str, Any]:
    state = _load_state(args.sandbox_id)
    if state.get("status") in {"completed", "stopped", "stopping", "terminated"}:
        _stop_relay_from_state(state)
        aws = _aws_cli()
        _verify_caller_account(aws, state)
        schedule_cleanup_error = _delete_expiry_schedule(
            aws,
            region=state["region"],
            profile=state["profile"],
            sandbox_id=state["sandbox_id"],
        )
        if schedule_cleanup_error:
            state["warnings"] = [
                f"expiry schedule cleanup failed: {schedule_cleanup_error}"
            ]
        else:
            state.pop("warnings", None)
        state["updated_at"] = time.time()
        _write_state(state)
        return _response(state)
    aws = _aws_cli()
    _verify_caller_account(aws, state)
    resource_type = state["resource_type"]
    if resource_type == "devicefarm":
        tags_payload = _run_aws(
            aws,
            region=state["region"],
            profile=state["profile"],
            command=[
                "devicefarm",
                "list-tags-for-resource",
                "--resource-arn",
                state["resource_id"],
            ],
        )
        _verify_ownership_tags(_devicefarm_tag_map(tags_payload), state)
        _stop_relay_from_state(state)
        stopped = _run_aws(
            aws,
            region=state["region"],
            profile=state["profile"],
            command=[
                "devicefarm",
                "stop-remote-access-session",
                "--arn",
                state["resource_id"],
            ],
        )
        session = stopped.get("remoteAccessSession")
        raw_status = session.get("status") if isinstance(session, dict) else None
        state["status"] = _normalize_status(raw_status, default="stopping")
    elif resource_type == "ec2":
        described = _run_aws(
            aws,
            region=state["region"],
            profile=state["profile"],
            command=["ec2", "describe-instances", "--instance-ids", state["resource_id"]],
        )
        instance = _single_described_instance(described)
        if instance.get("InstanceId") != state["resource_id"]:
            raise AwsSandboxError("EC2 instance ID does not match sandbox state")
        _verify_ownership_tags(_ec2_tag_map(instance), state)
        _stop_relay_from_state(state)
        terminated = _run_aws(
            aws,
            region=state["region"],
            profile=state["profile"],
            command=["ec2", "terminate-instances", "--instance-ids", state["resource_id"]],
        )
        transitions = terminated.get("TerminatingInstances")
        if not isinstance(transitions, list) or len(transitions) != 1:
            raise AwsSandboxError("EC2 terminate response did not contain one instance")
        transition = transitions[0]
        if not isinstance(transition, dict) or transition.get("InstanceId") != state["resource_id"]:
            raise AwsSandboxError("EC2 terminate response instance ID did not match")
        current = transition.get("CurrentState")
        raw_status = current.get("Name") if isinstance(current, dict) else None
        state["status"] = _normalize_status(raw_status, default="stopping")
    else:
        raise AwsSandboxError("sandbox state has an unsupported resource type")
    schedule_cleanup_error = _delete_expiry_schedule(
        aws,
        region=state["region"],
        profile=state["profile"],
        sandbox_id=state["sandbox_id"],
    )
    if schedule_cleanup_error:
        state["warnings"] = [
            f"expiry schedule cleanup failed: {schedule_cleanup_error}"
        ]
    else:
        state.pop("warnings", None)
    state["updated_at"] = time.time()
    _write_state(state)
    return _response(state)


def _cmd_logs(args: argparse.Namespace) -> dict[str, Any]:
    if args.tail <= 0 or args.tail > 10_000:
        raise AwsSandboxError("tail must be between 1 and 10000 lines")
    state = _load_state(args.sandbox_id)
    aws = _aws_cli()
    _verify_caller_account(aws, state)
    if state["resource_type"] == "devicefarm":
        _verify_devicefarm_ownership(aws, state)
        payload = _run_aws(
            aws,
            region=state["region"],
            profile=state["profile"],
            command=[
                "devicefarm",
                "get-remote-access-session",
                "--arn",
                state["resource_id"],
            ],
        )
        session = payload.get("remoteAccessSession")
        if not isinstance(session, dict):
            raise AwsSandboxError("Device Farm logs response omitted remoteAccessSession")
        raw_logs = session.get("message") if isinstance(session.get("message"), str) else ""
    elif state["resource_type"] == "ec2":
        _verify_ec2_ownership(aws, state)
        payload = _run_aws(
            aws,
            region=state["region"],
            profile=state["profile"],
            command=["ec2", "get-console-output", "--instance-id", state["resource_id"], "--latest"],
        )
        raw_logs = payload.get("Output") if isinstance(payload.get("Output"), str) else ""
    else:
        raise AwsSandboxError("sandbox state has an unsupported resource type")
    lines = _redact_sensitive_text(raw_logs).splitlines()
    result = _response(state)
    result["logs"] = "\n".join(lines[-args.tail :])
    return result


def _cmd_attach(args: argparse.Namespace) -> dict[str, Any]:
    state = _load_state(args.sandbox_id)
    aws = _aws_cli()
    _verify_caller_account(aws, state)
    if state["resource_type"] == "ec2":
        return _attach_pc_vm(args, state, aws)
    if state["resource_type"] != "devicefarm":
        raise AwsSandboxError("sandbox state has an unsupported resource type")
    _verify_devicefarm_ownership(aws, state)
    existing = state.get("relay")
    if isinstance(existing, dict) and _process_running(existing):
        result = _response(state)
        result["endpoint"] = _relay_endpoint(state)
        return result
    payload = _run_aws(
        aws,
        region=state["region"],
        profile=state["profile"],
        command=[
            "devicefarm",
            "get-remote-access-session",
            "--arn",
            state["resource_id"],
        ],
    )
    session = payload.get("remoteAccessSession")
    if not isinstance(session, dict):
        raise AwsSandboxError("Device Farm attach response omitted remoteAccessSession")
    if _normalize_status(session.get("status"), default="unknown") != "running":
        raise AwsSandboxError("Device Farm session is not running yet")
    endpoints = session.get("endpoints")
    if not isinstance(endpoints, dict):
        raise AwsSandboxError("Device Farm attach response omitted endpoints")
    upstream = endpoints.get("remoteDriverEndpoint")
    if not isinstance(upstream, str) or not upstream:
        raise AwsSandboxError("Device Farm attach response omitted remote driver endpoint")
    relay = _start_http_relay(upstream)
    endpoint = f"http://127.0.0.1:{relay['port']}"
    state["relay"] = relay
    state.pop("endpoint", None)
    state["updated_at"] = time.time()
    try:
        _write_state(state)
    except AwsSandboxError:
        _terminate_process(relay)
        raise
    result = _response(state)
    result["endpoint"] = endpoint
    return result


def _cmd_gc(args: argparse.Namespace) -> dict[str, Any]:
    expired: list[str] = []
    stopped: list[str] = []
    errors: dict[str, str] = {}
    now = time.time()
    for candidate in sorted(_state_root().iterdir(), key=lambda path: path.name):
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        sandbox_id = candidate.name
        try:
            state = _load_state(sandbox_id)
        except AwsSandboxError as exc:
            errors[sandbox_id] = _safe_error(str(exc))
            continue
        expires_at = state.get("expires_at")
        if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
            errors[sandbox_id] = "AWS sandbox state has an invalid expiry"
            continue
        if expires_at > now or state.get("status") in {"completed", "stopped", "terminated"}:
            continue
        expired.append(sandbox_id)
        if not args.execute:
            continue
        try:
            _locked_control(
                sandbox_id,
                _cmd_stop,
                argparse.Namespace(sandbox_id=sandbox_id),
            )
            stopped.append(sandbox_id)
        except AwsSandboxError as exc:
            errors[sandbox_id] = _safe_error(str(exc))
    return {
        "success": not errors,
        "provider": "aws",
        "status": "completed",
        "execute": bool(args.execute),
        "expired": expired,
        "stopped": stopped,
        "errors": errors,
    }


def _attach_pc_vm(
    args: argparse.Namespace,
    state: dict[str, Any],
    aws: str,
) -> dict[str, Any]:
    if args.local_port < 0 or not 1 <= args.rdp_port <= 65535:
        raise AwsSandboxError("SSM local port must be non-negative and RDP port must be valid")
    if args.startup_timeout <= 0 or args.startup_timeout > 300:
        raise AwsSandboxError("startup-timeout must be between 1 and 300 seconds")
    instance = _verify_ec2_ownership(aws, state)
    raw_state = instance.get("State")
    raw_status = raw_state.get("Name") if isinstance(raw_state, dict) else None
    if _normalize_status(raw_status, default="unknown") != "running":
        raise AwsSandboxError("EC2 instance is not running yet")
    existing = state.get("relay")
    if isinstance(existing, dict) and _process_running(existing):
        result = _response(state)
        result["endpoint"] = _relay_endpoint(state)
        return result
    local_port = args.local_port or _free_loopback_port()
    parameters = json.dumps(
        {
            "portNumber": [str(args.rdp_port)],
            "localPortNumber": [str(local_port)],
        },
        separators=(",", ":"),
    )
    command = _aws_argv(
        aws,
        region=state["region"],
        profile=state["profile"],
        command=[
            "ssm",
            "start-session",
            "--target",
            state["resource_id"],
            "--document-name",
            "AWS-StartPortForwardingSession",
            "--parameters",
            parameters,
        ],
        json_output=False,
    )
    process = _spawn_silent_detached(command)
    try:
        _wait_for_loopback_port(local_port, timeout=args.startup_timeout, process=process)
    except AwsSandboxError:
        _terminate_process(process)
        raise
    process.update({"kind": "ssm-rdp", "host": "127.0.0.1", "port": local_port})
    endpoint = f"rdp://127.0.0.1:{local_port}"
    state["relay"] = process
    state.pop("endpoint", None)
    state["updated_at"] = time.time()
    try:
        _write_state(state)
    except AwsSandboxError:
        _terminate_process(process)
        raise
    result = _response(state)
    result["endpoint"] = endpoint
    return result


def _start_android(args: argparse.Namespace) -> dict[str, Any]:
    if args.region != _DEFAULT_REGION:
        raise AwsSandboxError("AWS Device Farm remote access requires region us-west-2")
    project_arn = _required(args.project_arn, "Device Farm project ARN")
    device_arn = _required(args.device_arn, "Device Farm device ARN")
    aws = _aws_cli()
    account_id = _caller_account(aws, region=args.region, profile=args.profile)
    scheduler_role_arn, partition = _scheduler_role(
        args.scheduler_role_arn,
        account_id=account_id,
    )
    owner_id = _installation_id()
    sandbox_id = _new_sandbox_id("android")
    expires_at = int(time.time()) + args.ttl

    created = _run_aws(
        aws,
        region=args.region,
        profile=args.profile,
        command=[
            "devicefarm",
            "create-remote-access-session",
            "--project-arn",
            project_arn,
            "--device-arn",
            device_arn,
            "--name",
            sandbox_id,
            "--interaction-mode",
            args.interaction_mode,
        ],
    )
    session = created.get("remoteAccessSession")
    if not isinstance(session, dict):
        raise AwsSandboxError("Device Farm create response omitted remoteAccessSession")
    session_arn = session.get("arn")
    if not isinstance(session_arn, str) or not session_arn:
        raise AwsSandboxError("Device Farm create response omitted session ARN")

    tags = [
        {"Key": "agy:owner", "Value": owner_id},
        {"Key": "agy:sandbox-id", "Value": sandbox_id},
        {"Key": "agy:expires-at", "Value": str(expires_at)},
    ]
    try:
        _run_aws(
            aws,
            region=args.region,
            profile=args.profile,
            command=[
                "devicefarm",
                "tag-resource",
                "--resource-arn",
                session_arn,
                "--tags",
                json.dumps(tags, separators=(",", ":")),
            ],
        )
    except AwsSandboxError as exc:
        cleanup_error = _stop_new_devicefarm_session(
            aws,
            region=args.region,
            profile=args.profile,
            session_arn=session_arn,
        )
        _raise_if_cleanup_failed(
            exc,
            resource_label="Device Farm session",
            resource_id=session_arn,
            cleanup_error=cleanup_error,
        )
        raise

    try:
        _create_expiry_schedule(
            aws,
            region=args.region,
            profile=args.profile,
            partition=partition,
            account_id=account_id,
            scheduler_role_arn=scheduler_role_arn,
            sandbox_id=sandbox_id,
            expires_at=expires_at,
            resource_type="devicefarm",
            resource_id=session_arn,
        )
    except AwsSandboxError as exc:
        resource_cleanup_error = _stop_new_devicefarm_session(
            aws,
            region=args.region,
            profile=args.profile,
            session_arn=session_arn,
        )
        schedule_cleanup_error = _delete_expiry_schedule(
            aws,
            region=args.region,
            profile=args.profile,
            sandbox_id=sandbox_id,
        )
        _raise_if_cleanup_failed(
            exc,
            resource_label="Device Farm session",
            resource_id=session_arn,
            cleanup_error=_join_cleanup_errors(
                resource_cleanup_error,
                schedule_cleanup_error,
            ),
        )
        raise

    state = {
        "sandbox_id": sandbox_id,
        "provider": "aws",
        "target": "android-real",
        "resource_type": "devicefarm",
        "resource_id": session_arn,
        "region": args.region,
        "profile": args.profile,
        "account_id": account_id,
        "owner_id": owner_id,
        "status": _normalize_status(session.get("status"), default="pending"),
        "created_at": time.time(),
        "expires_at": expires_at,
        "expiry_enforcement": "eventbridge-scheduler",
    }
    try:
        _write_state(state)
    except AwsSandboxError as exc:
        resource_cleanup_error = _stop_new_devicefarm_session(
            aws,
            region=args.region,
            profile=args.profile,
            session_arn=session_arn,
        )
        schedule_cleanup_error = _delete_expiry_schedule(
            aws,
            region=args.region,
            profile=args.profile,
            sandbox_id=sandbox_id,
        )
        _raise_if_cleanup_failed(
            exc,
            resource_label="Device Farm session",
            resource_id=session_arn,
            cleanup_error=_join_cleanup_errors(
                resource_cleanup_error,
                schedule_cleanup_error,
            ),
        )
        raise
    return _response(state)


def _start_pc_vm(args: argparse.Namespace) -> dict[str, Any]:
    launch_template_id = _required(args.launch_template_id, "EC2 launch template ID")
    if not re.fullmatch(r"lt-[A-Za-z0-9]+", launch_template_id):
        raise AwsSandboxError("EC2 launch template ID is invalid")
    version = _required(args.launch_template_version, "EC2 launch template version")
    if not re.fullmatch(r"(?:\$Default|\$Latest|[0-9]+)", version):
        raise AwsSandboxError("EC2 launch template version is invalid")

    aws = _aws_cli()
    account_id = _caller_account(aws, region=args.region, profile=args.profile)
    scheduler_role_arn, partition = _scheduler_role(
        args.scheduler_role_arn,
        account_id=account_id,
    )
    owner_id = _installation_id()
    sandbox_id = _new_sandbox_id("pc")
    expires_at = int(time.time()) + args.ttl
    tags = [
        {"Key": "agy:owner", "Value": owner_id},
        {"Key": "agy:sandbox-id", "Value": sandbox_id},
        {"Key": "agy:expires-at", "Value": str(expires_at)},
    ]
    tag_specifications = json.dumps(
        [{"ResourceType": "instance", "Tags": tags}],
        separators=(",", ":"),
    )
    run_command = [
        "ec2",
        "run-instances",
        "--launch-template",
        f"LaunchTemplateId={launch_template_id},Version={version}",
        "--min-count",
        "1",
        "--max-count",
        "1",
        "--client-token",
        sandbox_id,
        "--instance-initiated-shutdown-behavior",
        "terminate",
        "--tag-specifications",
        tag_specifications,
    ]
    launched = _run_ec2_instance_with_reconciliation(
        aws,
        region=args.region,
        profile=args.profile,
        command=run_command,
        sandbox_id=sandbox_id,
        owner_id=owner_id,
        expires_at=expires_at,
    )
    instances = launched.get("Instances")
    if not isinstance(instances, list) or len(instances) != 1 or not isinstance(instances[0], dict):
        raise AwsSandboxError("EC2 run-instances response did not contain exactly one instance")
    instance = instances[0]
    instance_id = instance.get("InstanceId")
    if not isinstance(instance_id, str) or not re.fullmatch(r"i-[A-Za-z0-9]+", instance_id):
        raise AwsSandboxError("EC2 run-instances response omitted a valid instance ID")
    raw_state = instance.get("State")
    raw_status = raw_state.get("Name") if isinstance(raw_state, dict) else None
    try:
        _create_expiry_schedule(
            aws,
            region=args.region,
            profile=args.profile,
            partition=partition,
            account_id=account_id,
            scheduler_role_arn=scheduler_role_arn,
            sandbox_id=sandbox_id,
            expires_at=expires_at,
            resource_type="ec2",
            resource_id=instance_id,
        )
    except AwsSandboxError as exc:
        resource_cleanup_error = _terminate_new_ec2_instance(
            aws,
            region=args.region,
            profile=args.profile,
            instance_id=instance_id,
        )
        schedule_cleanup_error = _delete_expiry_schedule(
            aws,
            region=args.region,
            profile=args.profile,
            sandbox_id=sandbox_id,
        )
        _raise_if_cleanup_failed(
            exc,
            resource_label="EC2 instance",
            resource_id=instance_id,
            cleanup_error=_join_cleanup_errors(
                resource_cleanup_error,
                schedule_cleanup_error,
            ),
        )
        raise

    state = {
        "sandbox_id": sandbox_id,
        "provider": "aws",
        "target": "pc-vm",
        "resource_type": "ec2",
        "resource_id": instance_id,
        "region": args.region,
        "profile": args.profile,
        "account_id": account_id,
        "owner_id": owner_id,
        "status": _normalize_status(raw_status, default="pending"),
        "created_at": time.time(),
        "expires_at": expires_at,
        "expiry_enforcement": "eventbridge-scheduler",
    }
    try:
        _write_state(state)
    except AwsSandboxError as exc:
        resource_cleanup_error = _terminate_new_ec2_instance(
            aws,
            region=args.region,
            profile=args.profile,
            instance_id=instance_id,
        )
        schedule_cleanup_error = _delete_expiry_schedule(
            aws,
            region=args.region,
            profile=args.profile,
            sandbox_id=sandbox_id,
        )
        _raise_if_cleanup_failed(
            exc,
            resource_label="EC2 instance",
            resource_id=instance_id,
            cleanup_error=_join_cleanup_errors(
                resource_cleanup_error,
                schedule_cleanup_error,
            ),
        )
        raise
    return _response(state)


def _run_ec2_instance_with_reconciliation(
    aws: str,
    *,
    region: str,
    profile: str,
    command: list[str],
    sandbox_id: str,
    owner_id: str,
    expires_at: int,
) -> dict[str, Any]:
    failures: list[str] = []
    for _ in range(2):
        try:
            return _run_aws(
                aws,
                region=region,
                profile=profile,
                command=command,
            )
        except AwsSandboxError as exc:
            failures.append(_safe_error(str(exc)))

    filters = json.dumps(
        [
            {"Name": "client-token", "Values": [sandbox_id]},
            {"Name": "tag:agy:owner", "Values": [owner_id]},
            {"Name": "tag:agy:sandbox-id", "Values": [sandbox_id]},
            {"Name": "tag:agy:expires-at", "Values": [str(expires_at)]},
        ],
        separators=(",", ":"),
    )
    try:
        described = _run_aws(
            aws,
            region=region,
            profile=profile,
            command=[
                "ec2",
                "describe-instances",
                "--filters",
                filters,
            ],
        )
    except AwsSandboxError as exc:
        failures.append(_safe_error(str(exc)))
        detail = "; ".join(failures)
        raise AwsSandboxError(
            f"EC2 create result is ambiguous for client token {sandbox_id}; "
            f"reconciliation failed: {detail}"
        ) from exc

    instances = _all_described_instances(described)
    if len(instances) != 1:
        detail = "; ".join(failures)
        raise AwsSandboxError(
            f"EC2 create result is ambiguous for client token {sandbox_id}; "
            f"reconciliation found {len(instances)} owned instances after: {detail}"
        )
    instance = instances[0]
    if instance.get("ClientToken") != sandbox_id:
        raise AwsSandboxError("reconciled EC2 instance client token does not match")
    _verify_ownership_tags(
        _ec2_tag_map(instance),
        {
            "owner_id": owner_id,
            "sandbox_id": sandbox_id,
            "expires_at": expires_at,
        },
    )
    return {"Instances": [instance]}


def _all_described_instances(payload: dict[str, Any]) -> list[dict[str, Any]]:
    reservations = payload.get("Reservations")
    if not isinstance(reservations, list):
        raise AwsSandboxError("EC2 reconciliation response omitted reservations")
    instances: list[dict[str, Any]] = []
    for reservation in reservations:
        if not isinstance(reservation, dict):
            continue
        raw_instances = reservation.get("Instances")
        if not isinstance(raw_instances, list):
            continue
        instances.extend(
            instance for instance in raw_instances if isinstance(instance, dict)
        )
    return instances


def _scheduler_role(value: str, *, account_id: str) -> tuple[str, str]:
    role_arn = _required(value, "Scheduler execution role ARN")
    match = _SCHEDULER_ROLE_ARN_RE.fullmatch(role_arn)
    if match is None:
        raise AwsSandboxError("Scheduler execution role ARN is invalid")
    if match.group(2) != account_id:
        raise AwsSandboxError(
            "Scheduler execution role must belong to the current AWS account"
        )
    return role_arn, match.group(1)


def _create_expiry_schedule(
    aws: str,
    *,
    region: str,
    profile: str,
    partition: str,
    account_id: str,
    scheduler_role_arn: str,
    sandbox_id: str,
    expires_at: int,
    resource_type: str,
    resource_id: str,
) -> None:
    if resource_type == "ec2":
        service_action = "ec2:terminateInstances"
        target_input = {"InstanceIds": [resource_id]}
    elif resource_type == "devicefarm":
        service_action = "devicefarm:stopRemoteAccessSession"
        target_input = {"arn": resource_id}
    else:
        raise AwsSandboxError("unsupported resource type for expiry schedule")
    target = {
        "Arn": f"arn:{partition}:scheduler:::aws-sdk:{service_action}",
        "RoleArn": scheduler_role_arn,
        "Input": json.dumps(target_input, separators=(",", ":")),
        "RetryPolicy": {
            "MaximumEventAgeInSeconds": 86400,
            "MaximumRetryAttempts": 185,
        },
    }
    expires = datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    payload = _run_aws(
        aws,
        region=region,
        profile=profile,
        command=[
            "scheduler",
            "create-schedule",
            "--name",
            sandbox_id,
            "--schedule-expression",
            f"at({expires})",
            "--schedule-expression-timezone",
            "UTC",
            "--flexible-time-window",
            '{"Mode":"OFF"}',
            "--action-after-completion",
            "DELETE",
            "--client-token",
            sandbox_id,
            "--target",
            json.dumps(target, separators=(",", ":")),
        ],
    )
    schedule_arn = payload.get("ScheduleArn")
    expected_prefix = (
        f"arn:{partition}:scheduler:{region}:{account_id}:schedule/default/"
    )
    if (
        not isinstance(schedule_arn, str)
        or schedule_arn != f"{expected_prefix}{sandbox_id}"
    ):
        raise AwsSandboxError("Scheduler create response omitted the expected schedule ARN")


def _delete_expiry_schedule(
    aws: str,
    *,
    region: str,
    profile: str,
    sandbox_id: str,
) -> str | None:
    try:
        _run_aws(
            aws,
            region=region,
            profile=profile,
            command=[
                "scheduler",
                "delete-schedule",
                "--name",
                sandbox_id,
            ],
        )
    except AwsSandboxError as exc:
        detail = _safe_error(str(exc))
        if "ResourceNotFoundException" in detail:
            return None
        return detail
    return None


def _caller_account(aws: str, *, region: str, profile: str) -> str:
    identity = _run_aws(
        aws,
        region=region,
        profile=profile,
        command=["sts", "get-caller-identity"],
    )
    account_id = identity.get("Account")
    if not isinstance(account_id, str) or not _ACCOUNT_ID_RE.fullmatch(account_id):
        raise AwsSandboxError("AWS caller identity omitted a valid account ID")
    return account_id


def _verify_caller_account(aws: str, state: dict[str, Any]) -> None:
    actual = _caller_account(aws, region=state["region"], profile=state["profile"])
    if actual != state["account_id"]:
        raise AwsSandboxError("current AWS account does not match sandbox owner account")


def _single_described_instance(payload: dict[str, Any]) -> dict[str, Any]:
    reservations = payload.get("Reservations")
    if not isinstance(reservations, list) or len(reservations) != 1:
        raise AwsSandboxError("EC2 describe response did not contain one reservation")
    reservation = reservations[0]
    if not isinstance(reservation, dict):
        raise AwsSandboxError("EC2 describe response contained an invalid reservation")
    instances = reservation.get("Instances")
    if not isinstance(instances, list) or len(instances) != 1 or not isinstance(instances[0], dict):
        raise AwsSandboxError("EC2 describe response did not contain exactly one instance")
    return instances[0]


def _ec2_tag_map(instance: dict[str, Any]) -> dict[str, str]:
    return _tag_list_to_map(instance.get("Tags"), service="EC2")


def _devicefarm_tag_map(payload: dict[str, Any]) -> dict[str, str]:
    return _tag_list_to_map(payload.get("Tags"), service="Device Farm")


def _tag_list_to_map(raw_tags: Any, *, service: str) -> dict[str, str]:
    if not isinstance(raw_tags, list):
        raise AwsSandboxError(f"{service} ownership tags are missing")
    tags: dict[str, str] = {}
    for raw_tag in raw_tags:
        if not isinstance(raw_tag, dict):
            continue
        key = raw_tag.get("Key")
        value = raw_tag.get("Value")
        if isinstance(key, str) and isinstance(value, str):
            tags[key] = value
    return tags


def _verify_devicefarm_ownership(aws: str, state: dict[str, Any]) -> None:
    payload = _run_aws(
        aws,
        region=state["region"],
        profile=state["profile"],
        command=[
            "devicefarm",
            "list-tags-for-resource",
            "--resource-arn",
            state["resource_id"],
        ],
    )
    _verify_ownership_tags(_devicefarm_tag_map(payload), state)


def _verify_ec2_ownership(aws: str, state: dict[str, Any]) -> dict[str, Any]:
    payload = _run_aws(
        aws,
        region=state["region"],
        profile=state["profile"],
        command=["ec2", "describe-instances", "--instance-ids", state["resource_id"]],
    )
    instance = _single_described_instance(payload)
    if instance.get("InstanceId") != state["resource_id"]:
        raise AwsSandboxError("EC2 instance ID does not match sandbox state")
    _verify_ownership_tags(_ec2_tag_map(instance), state)
    return instance


def _verify_ownership_tags(tags: dict[str, str], state: dict[str, Any]) -> None:
    expected = {
        "agy:owner": state["owner_id"],
        "agy:sandbox-id": state["sandbox_id"],
        "agy:expires-at": str(state["expires_at"]),
    }
    if any(tags.get(key) != value for key, value in expected.items()):
        raise AwsSandboxError("remote resource ownership tags do not match sandbox state")


class _DeviceFarmRelayHandler(BaseHTTPRequestHandler):
    upstream_url = ""

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def do_OPTIONS(self) -> None:
        self._proxy()

    def _proxy(self) -> None:
        content_length = self.headers.get("Content-Length", "0")
        try:
            body_length = int(content_length)
        except ValueError:
            self.send_error(400)
            return
        if body_length < 0 or body_length > _RELAY_MAX_BODY_BYTES:
            self.send_error(413)
            return
        body = self.rfile.read(body_length) if body_length else None
        target = _relay_target_url(self.upstream_url, self.path)
        headers = {
            name: self.headers[name]
            for name in ("Accept", "Content-Type", "User-Agent")
            if self.headers.get(name)
        }
        request = urllib_request.Request(
            target,
            data=body,
            headers=headers,
            method=self.command,
        )
        opener = urllib_request.build_opener(
            urllib_request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        try:
            response = opener.open(request, timeout=_RELAY_TIMEOUT_SECONDS)
        except urllib_error.HTTPError as exc:
            response = exc
        except (OSError, urllib_error.URLError):
            self.send_error(502)
            return
        try:
            response_body = response.read(_RELAY_MAX_BODY_BYTES + 1)
            if len(response_body) > _RELAY_MAX_BODY_BYTES:
                self.send_error(502)
                return
            content_type = response.headers.get("Content-Type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if (
                media_type.startswith("text/")
                or media_type in {"application/json", "application/xml"}
                or media_type.endswith("+json")
                or media_type.endswith("+xml")
            ):
                response_body = _redact_sensitive_text(
                    response_body.decode("utf-8", errors="replace")
                ).encode("utf-8")
            self.send_response(response.status)
            for name in ("Content-Type", "Cache-Control"):
                value = response.headers.get(name)
                if value:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
        finally:
            response.close()

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _relay_target_url(upstream_url: str, client_path: str) -> str:
    upstream = urllib_parse.urlsplit(upstream_url)
    client = urllib_parse.urlsplit(client_path)
    base_path = upstream.path.rstrip("/")
    suffix = client.path.lstrip("/")
    path = f"{base_path}/{suffix}" if suffix else (base_path or "/")
    query = "&".join(part for part in (upstream.query, client.query) if part)
    return urllib_parse.urlunsplit((upstream.scheme, upstream.netloc, path, query, ""))


def _start_http_relay(upstream_url: str) -> dict[str, Any]:
    if os.name == "nt" or not hasattr(os, "fork"):
        raise AwsSandboxError("Device Farm attach relay currently requires a POSIX host")
    parsed = urllib_parse.urlsplit(upstream_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise AwsSandboxError("Device Farm returned an invalid remote driver endpoint")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise AwsSandboxError(f"failed to fork Device Farm relay: {_safe_error(str(exc))}")
    if pid == 0:
        os.close(read_fd)
        try:
            os.setsid()
            devnull = os.open(os.devnull, os.O_RDWR)
            try:
                os.dup2(devnull, 0)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
            finally:
                if devnull > 2:
                    os.close(devnull)
            handler = type(
                "DeviceFarmRelayHandler",
                (_DeviceFarmRelayHandler,),
                {"upstream_url": upstream_url},
            )
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
            server.daemon_threads = True
            os.write(write_fd, b"1")
            os.close(write_fd)
            server.serve_forever(poll_interval=0.2)
        except BaseException:
            try:
                os.write(write_fd, b"0")
            except OSError:
                pass
            os._exit(1)
        os._exit(0)
    os.close(write_fd)
    ready, _, _ = select.select([read_fd], [], [], 10)
    marker = os.read(read_fd, 1) if ready else b""
    os.close(read_fd)
    if marker != b"1":
        _terminate_pid(pid, pgid=pid)
        raise AwsSandboxError("Device Farm relay failed to bind its loopback port")
    start_token = _process_start_token(pid)
    if not start_token:
        _terminate_pid(pid, pgid=pid)
        raise AwsSandboxError("could not record Device Farm relay process identity")
    return {
        "kind": "devicefarm-http",
        "pid": pid,
        "pgid": pid,
        "start_token": start_token,
        "host": "127.0.0.1",
        "port": port,
    }


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _spawn_silent_detached(command: list[str]) -> dict[str, Any]:
    if os.name == "nt" or not hasattr(os, "fork"):
        raise AwsSandboxError("SSM attach currently requires a POSIX host")
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise AwsSandboxError(f"failed to fork SSM tunnel: {_safe_error(str(exc))}")
    if pid == 0:
        os.close(read_fd)
        try:
            os.setsid()
            devnull = os.open(os.devnull, os.O_RDWR)
            try:
                os.dup2(devnull, 0)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
            finally:
                if devnull > 2:
                    os.close(devnull)
            os.execvpe(command[0], command, _aws_environment())
        except BaseException as exc:
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
        raise AwsSandboxError(
            f"failed to start SSM tunnel: {_safe_error(error.decode('utf-8', errors='replace'))}"
        )
    start_token = _process_start_token(pid)
    if not start_token:
        _terminate_pid(pid, pgid=pid)
        raise AwsSandboxError("could not record SSM tunnel process identity")
    return {"pid": pid, "pgid": pid, "start_token": start_token}


def _wait_for_loopback_port(
    port: int,
    *,
    timeout: int,
    process: dict[str, Any],
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_running(process):
            raise AwsSandboxError("SSM tunnel exited before opening its loopback port")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise AwsSandboxError("timed out waiting for SSM loopback port")


def _stop_relay_from_state(state: dict[str, Any]) -> None:
    relay = state.get("relay")
    if isinstance(relay, dict):
        _terminate_process(relay)
    state.pop("relay", None)
    state.pop("endpoint", None)


def _process_running(process: dict[str, Any]) -> bool:
    pid = process.get("pid")
    pgid = process.get("pgid")
    token = process.get("start_token")
    basic_identity_matches = (
        isinstance(pid, int)
        and pid > 0
        and isinstance(token, str)
        and bool(token)
        and _pid_running(pid)
        and _process_start_token(pid) == token
    )
    if not basic_identity_matches:
        return False
    if os.name != "nt":
        if not isinstance(pgid, int) or pgid <= 0:
            return False
        try:
            return os.getpgid(pid) == pgid
        except OSError:
            return False
    return True


def _terminate_process(process: dict[str, Any]) -> bool:
    if not _process_running(process):
        return False
    pid = int(process["pid"])
    pgid = process.get("pgid")
    return _terminate_pid(pid, pgid=int(pgid) if isinstance(pgid, int) else None)


def _terminate_pid(pid: int, *, pgid: int | None = None) -> bool:
    if pid <= 0 or not _pid_running(pid):
        return False
    try:
        os.killpg(pgid or pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return False
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _pid_running(pid):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pgid or pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    return True


def _pid_running(pid: int) -> bool:
    try:
        waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        if waited_pid == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _process_start_token(pid: int) -> str:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        fields = text.rsplit(") ", 1)[1].split()
        if len(fields) > 19:
            return f"proc:{fields[19]}"
    except (IndexError, OSError):
        pass
    ps = shutil.which("ps")
    if not ps:
        return ""
    utility_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"LANG", "LC_ALL", "LC_CTYPE", "PATH"} and value
    }
    try:
        result = subprocess.run(
            [ps, "-p", str(pid), "-o", "lstart="],
            env=utility_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode == 0 and result.stdout.strip():
        return f"ps:{result.stdout.strip()}"
    return ""


def _stop_new_devicefarm_session(
    aws: str,
    *,
    region: str,
    profile: str,
    session_arn: str,
) -> str | None:
    try:
        _run_aws(
            aws,
            region=region,
            profile=profile,
            command=[
                "devicefarm",
                "stop-remote-access-session",
                "--arn",
                session_arn,
            ],
        )
    except AwsSandboxError as exc:
        return _safe_error(str(exc))
    return None


def _terminate_new_ec2_instance(
    aws: str,
    *,
    region: str,
    profile: str,
    instance_id: str,
) -> str | None:
    try:
        _run_aws(
            aws,
            region=region,
            profile=profile,
            command=["ec2", "terminate-instances", "--instance-ids", instance_id],
        )
    except AwsSandboxError as exc:
        return _safe_error(str(exc))
    return None


def _raise_if_cleanup_failed(
    original: AwsSandboxError,
    *,
    resource_label: str,
    resource_id: str,
    cleanup_error: str | None,
) -> None:
    if cleanup_error is None:
        return
    detail = (
        f"{_safe_error(str(original))}; cleanup failed for {resource_label} "
        f"{_safe_error(resource_id)}: {_safe_error(cleanup_error)}"
    )
    raise AwsSandboxError(detail) from original


def _join_cleanup_errors(*errors: str | None) -> str | None:
    failures = [error for error in errors if error]
    if not failures:
        return None
    return "; ".join(failures)


def _canonical_target(target: str) -> str:
    normalized = target.strip().lower()
    if normalized in _ANDROID_TARGETS:
        return "android-real"
    if normalized in _PC_VM_TARGETS:
        return "pc-vm"
    raise AwsSandboxError("target must be android-real/mobile or pc-vm/desktop")


def _required(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise AwsSandboxError(f"{label} is required")
    if "\x00" in normalized or "\n" in normalized or "\r" in normalized:
        raise AwsSandboxError(f"{label} must not contain control characters")
    return normalized


def _env_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        raise AwsSandboxError(f"{name} must be an integer")


def _aws_cli() -> str:
    configured = os.environ.get("AGY_AWS_CLI", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AwsSandboxError("AGY_AWS_CLI does not point to an executable file")
        return str(path.resolve())
    resolved = shutil.which("aws")
    if not resolved:
        raise AwsSandboxError("AWS CLI not found; install AWS CLI v2 or set AGY_AWS_CLI")
    return resolved


def _run_aws(
    aws: str,
    *,
    region: str,
    profile: str,
    command: list[str],
) -> dict[str, Any]:
    argv = _aws_argv(
        aws,
        region=region,
        profile=profile,
        command=command,
        json_output=True,
    )
    try:
        result = subprocess.run(
            argv,
            env=_aws_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=_AWS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AwsSandboxError(f"AWS CLI failed to run: {_safe_error(str(exc))}")
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise AwsSandboxError(f"AWS CLI failed: {_safe_error(detail)}")
    if not result.stdout.strip():
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AwsSandboxError("AWS CLI returned invalid JSON")
    if not isinstance(payload, dict):
        raise AwsSandboxError("AWS CLI returned a non-object JSON response")
    return payload


def _aws_argv(
    aws: str,
    *,
    region: str,
    profile: str,
    command: list[str],
    json_output: bool,
) -> list[str]:
    argv = [aws, "--region", region]
    if profile:
        argv.extend(["--profile", profile])
    if json_output:
        argv.extend(["--output", "json", "--no-cli-pager"])
    argv.extend(command)
    return argv


def _aws_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _AWS_ENV_ALLOWLIST and value and value != "***"
    }
    relative_uri = os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "")
    if relative_uri and relative_uri != "***":
        if not _safe_container_credentials_relative_uri(relative_uri):
            raise AwsSandboxError(
                "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI must be an absolute path"
            )
        environment["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"] = relative_uri

    full_uri = os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI", "")
    if full_uri and full_uri != "***":
        if not _safe_container_credentials_uri(full_uri):
            raise AwsSandboxError(
                "AWS_CONTAINER_CREDENTIALS_FULL_URI is not a permitted container endpoint"
            )
        environment["AWS_CONTAINER_CREDENTIALS_FULL_URI"] = full_uri
        token_file = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "")
        if token_file and token_file != "***":
            if (
                not Path(token_file).is_absolute()
                or any(character in token_file for character in "\x00\r\n")
            ):
                raise AwsSandboxError(
                    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE must be an absolute path"
                )
            environment["AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"] = token_file
    return environment


def _safe_container_credentials_relative_uri(value: str) -> bool:
    if any(character in value for character in "\x00\r\n"):
        return False
    parsed = urllib_parse.urlsplit(value)
    return bool(
        value.startswith("/")
        and not value.startswith("//")
        and not parsed.scheme
        and not parsed.netloc
        and not parsed.query
        and not parsed.fragment
    )


def _safe_container_credentials_uri(value: str) -> bool:
    if not value or value == "***" or any(
        character in value for character in "\x00\r\n"
    ):
        return False
    try:
        parsed = urllib_parse.urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not parsed.hostname
        ):
            return False
        _ = parsed.port
        if parsed.hostname.lower() == "localhost":
            return True
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return False
    permitted = {
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("169.254.170.23"),
        ipaddress.ip_address("fd00:ec2::23"),
    }
    return address.is_loopback or address in permitted


def _safe_error(value: str) -> str:
    return _redact_sensitive_text(value)[:4096]


def _redact_sensitive_text(value: str) -> str:
    redacted = redact_text(value)
    return _SIGNED_QUERY_RE.sub(r"\1***", redacted)


def _new_sandbox_id(kind: str) -> str:
    return f"aws-{kind}-{int(time.time())}-{secrets.token_hex(4)}"


def _state_root() -> Path:
    override = os.environ.get("AGY_AWS_SANDBOX_STATE_DIR", "")
    root = Path(override).expanduser() if override else Path.home() / ".agy-mcp" / "aws-sandboxes"
    _ensure_private_dir(root)
    return root


def _ensure_private_dir(path: Path) -> None:
    if path.is_symlink():
        raise AwsSandboxError(f"state directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _installation_id() -> str:
    path = _state_root() / "owner-id"
    if path.is_symlink():
        raise AwsSandboxError("owner-id must not be a symlink")
    try:
        with path.open("x", encoding="utf-8") as handle:
            value = secrets.token_hex(16)
            handle.write(value)
        path.chmod(0o600)
        return value
    except FileExistsError:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AwsSandboxError(f"owner-id is unreadable: {_safe_error(str(exc))}")
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise AwsSandboxError("owner-id is invalid")
        return value


def _state_path(sandbox_id: str) -> Path:
    if not _SANDBOX_ID_RE.fullmatch(sandbox_id) or sandbox_id in {".", ".."}:
        raise AwsSandboxError("sandbox id is invalid")
    directory = _state_root() / sandbox_id
    _ensure_private_dir(directory)
    return directory / "state.json"


def _locked_control(
    sandbox_id: str,
    operation,
    args: argparse.Namespace,
) -> dict[str, Any]:
    with _sandbox_lock(sandbox_id):
        return operation(args)


@contextmanager
def _sandbox_lock(sandbox_id: str) -> Iterator[None]:
    if not _SANDBOX_ID_RE.fullmatch(sandbox_id) or sandbox_id in {".", ".."}:
        raise AwsSandboxError("sandbox id is invalid")
    directory = _state_root() / sandbox_id
    _ensure_private_dir(directory)
    path = directory / ".lock"
    if path.is_symlink():
        raise AwsSandboxError("sandbox lock file must not be a symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise AwsSandboxError(f"failed to open sandbox lock: {_safe_error(str(exc))}")
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write_state(state: dict[str, Any]) -> None:
    sandbox_id = str(state.get("sandbox_id", ""))
    path = _state_path(sandbox_id)
    if path.is_symlink():
        raise AwsSandboxError("state file must not be a symlink")
    temp_path = path.with_name(f".state-{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temp_path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise AwsSandboxError(f"failed to write state: {_safe_error(str(exc))}")


def _load_state(sandbox_id: str) -> dict[str, Any]:
    if not _SANDBOX_ID_RE.fullmatch(sandbox_id) or sandbox_id in {".", ".."}:
        raise AwsSandboxError("sandbox id is invalid")
    directory = _state_root() / sandbox_id
    if directory.is_symlink():
        raise AwsSandboxError("sandbox state directory must not be a symlink")
    path = directory / "state.json"
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        raise AwsSandboxError(f"AWS sandbox not found: {sandbox_id}")
    except (OSError, json.JSONDecodeError) as exc:
        raise AwsSandboxError(f"AWS sandbox state is unreadable: {_safe_error(str(exc))}")
    if not isinstance(state, dict):
        raise AwsSandboxError("AWS sandbox state is invalid")
    required_strings = (
        "sandbox_id",
        "target",
        "resource_type",
        "resource_id",
        "region",
        "profile",
        "account_id",
        "owner_id",
    )
    if any(not isinstance(state.get(key), str) for key in required_strings):
        raise AwsSandboxError("AWS sandbox state is missing required fields")
    if state["sandbox_id"] != sandbox_id:
        raise AwsSandboxError("AWS sandbox state ID does not match its path")
    if not _ACCOUNT_ID_RE.fullmatch(state["account_id"]):
        raise AwsSandboxError("AWS sandbox state account ID is invalid")
    if not re.fullmatch(r"[a-f0-9]{32}", state["owner_id"]):
        raise AwsSandboxError("AWS sandbox state owner ID is invalid")
    return state


def _normalize_status(value: Any, *, default: str) -> str:
    if isinstance(value, str) and value.strip():
        normalized = value.strip().lower().replace("_", "-")
        if normalized in {"shutting-down", "stopping"}:
            return "stopping"
        if normalized in {"terminated", "completed", "stopped"}:
            return "stopped"
        return normalized
    return default


def _response(state: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "resource_type": state["resource_type"],
        "resource_id": state["resource_id"],
        "region": state["region"],
        "account_id": state["account_id"],
        "expires_at": state["expires_at"],
        "expiry_enforcement": state.get("expiry_enforcement"),
    }
    result = {
        "success": True,
        "provider": "aws",
        "target": state["target"],
        "sandbox_id": state["sandbox_id"],
        "status": state["status"],
        "endpoint": _relay_endpoint(state),
        "metadata": metadata,
    }
    warnings = state.get("warnings")
    if isinstance(warnings, list) and all(
        isinstance(warning, str) for warning in warnings
    ):
        result["warnings"] = warnings
    return result


def _relay_endpoint(state: dict[str, Any]) -> str | None:
    relay = state.get("relay")
    if not isinstance(relay, dict) or not _process_running(relay):
        return None
    if relay.get("host") != "127.0.0.1":
        return None
    port = relay.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    if relay.get("kind") == "devicefarm-http":
        return f"http://127.0.0.1:{port}"
    if relay.get("kind") == "ssm-rdp":
        return f"rdp://127.0.0.1:{port}"
    return None


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def _emit_error(exc: Exception, *, json_output: bool) -> None:
    message = _safe_error(str(exc))
    if json_output:
        print(json.dumps({"success": False, "status": "error", "error": message}), file=sys.stderr)
    else:
        print(f"agy-aws-sandbox failed: {message}", file=sys.stderr)


__all__ = ["AwsSandboxError", "main"]
