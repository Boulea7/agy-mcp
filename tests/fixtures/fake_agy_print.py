"""Fake `agy --print` CLI used by adapter tests.

Mimics agy v1.0.0 surface that AgyPrintBackend depends on:

* `--help` prints flag list that exercises `detect_flags()`.
* `--version` prints a bare semver line.
* `--print <prompt>` prints a plain-text reply to stdout and exits 0.
* `--log-file <path>` writes a minimal klog so the tail loop has nothing to do.

Optional env vars steer error scenarios:

* ``FAKE_AGY_EXIT``        — exit code to return (default 0).
* ``FAKE_AGY_REPLY``       — body for the stdout reply.
* ``FAKE_AGY_STDERR``      — extra string to print on stderr.
* ``FAKE_AGY_SLEEP``       — seconds to sleep before printing (for timeout tests).
* ``FAKE_AGY_PRE_STDOUT``  — chunks to print before the reply (used to test
                             stdout buffering across multiple lines).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


HELP_TEXT = """\
agy [flags] [prompt]

Flags:
  --print              Run a non-interactive print.
  -p, --prompt         Alias for --print.
  --print-timeout      Maximum duration before the inner CLI aborts.
  --conversation       Resume an existing conversation by id.
  -c, --continue       Continue the most recent conversation.
  --sandbox            Enable sandbox mode.
  --log-file           Write klog operations log to the given path.
  --add-dir            Whitelist an additional working directory.
  --dangerously-skip-permissions  Bypass workspace permission checks.
  --help               Show this help text.
  --version            Print version and exit.
"""


def _emit_klog(log_path: Path, prompt: str) -> None:
    """Write a tiny but well-formed klog stream so the tail loop has data."""

    convo = "12345678-aaaa-bbbb-cccc-1234567890ab"
    lines = [
        "I0520 12:00:00.000123  1234 cli.go:42] Print mode: starting"
        f' (promptLength={len(prompt)}, model="gemini-3-pro", conversationID="{convo}")',
        "I0520 12:00:00.000200  1234 sidecar.go:88] Language server listening on random port at 60000 for HTTPS (gRPC)",
        f"I0520 12:00:00.000300  1234 conv.go:10] Created conversation {convo}",
        "I0520 12:00:00.001000  1234 conv.go:50] Stopping conversation stream",
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--print", dest="print_prompt", default=None)
    parser.add_argument("-p", "--prompt", dest="alt_prompt", default=None)
    parser.add_argument("--print-timeout", dest="print_timeout", default=None)
    parser.add_argument("--conversation", dest="conversation", default=None)
    parser.add_argument("-c", "--continue", dest="cont", action="store_true")
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--log-file", dest="log_file", default=None)
    parser.add_argument("--add-dir", dest="add_dir", default=None)
    parser.add_argument(
        "--dangerously-skip-permissions", dest="skip_perms", action="store_true"
    )
    parser.add_argument("positional", nargs="*")
    args = parser.parse_args(argv)

    if args.help:
        sys.stdout.write(HELP_TEXT)
        return 0
    if args.version:
        sys.stdout.write("1.0.0-fake\n")
        return 0

    prompt = args.print_prompt or args.alt_prompt or (
        args.positional[0] if args.positional else ""
    )
    # Use a test-only prefix that does NOT match the safety SECRET_ENV_NAME
    # pattern (contains no secret-word substrings) — otherwise the adapter's
    # env scrub would redact our signal before the subprocess starts.
    pre_chunks = os.environ.get("AGY_TEST_PRE_STDOUT", "")
    reply = os.environ.get("AGY_TEST_REPLY", f"echo: {prompt}")
    stderr_extra = os.environ.get("AGY_TEST_STDERR", "")
    sleep_s = float(os.environ.get("AGY_TEST_SLEEP", "0") or 0)
    exit_code = int(os.environ.get("AGY_TEST_EXIT", "0") or 0)

    if args.log_file:
        _emit_klog(Path(args.log_file), prompt)

    if pre_chunks:
        sys.stdout.write(pre_chunks)
        sys.stdout.flush()

    if sleep_s > 0:
        time.sleep(sleep_s)

    sys.stdout.write(reply)
    if not reply.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()

    if stderr_extra:
        sys.stderr.write(stderr_extra)
        sys.stderr.flush()

    return exit_code


if __name__ == "__main__":  # pragma: no cover - executed via subprocess
    raise SystemExit(main())
