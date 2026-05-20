"""Fake agy that streams a richer klog so adapter tail logic gets exercised.

Differs from ``fake_agy_print``:

* Writes klog lines incrementally (with small sleeps) so the tail thread sees
  them appear over time — exactly how real agy behaves.
* Includes the full lifecycle: sidecar port → created conversation → print
  start → optional rewind → stopping stream.
* Emits a configurable ``conversation_id`` so tests can assert session_id
  promotion from klog.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HELP_TEXT = """\
agy [flags]

Flags:
  --print
  -p, --prompt
  --print-timeout
  --conversation
  --continue, -c
  --sandbox
  --log-file
  --add-dir
  --dangerously-skip-permissions
  --help
  --version
"""


def _write_line(fp, line: str) -> None:
    fp.write(line + "\n")
    fp.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--print", dest="prompt", default=None)
    parser.add_argument("-p", "--prompt", dest="alt_prompt", default=None)
    parser.add_argument("--print-timeout", dest="print_timeout", default=None)
    parser.add_argument("--conversation", dest="conversation", default=None)
    parser.add_argument("-c", "--continue", dest="cont", action="store_true")
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--log-file", dest="log_file", default=None)
    parser.add_argument("--add-dir", dest="add_dir", default=None)
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("positional", nargs="*")
    args = parser.parse_args(argv)

    if args.help:
        sys.stdout.write(HELP_TEXT)
        return 0
    if args.version:
        sys.stdout.write("1.0.0-fake-rich\n")
        return 0

    prompt = args.prompt or args.alt_prompt or (
        args.positional[0] if args.positional else ""
    )
    # Conversation ids in real agy are UUIDs; the parser regex requires hex
    # chars (or dashes), so keep this fixture honest.
    convo = args.conversation or os.environ.get(
        "AGY_TEST_CONV", "abcdef01-2345-6789-abcd-ef0123456789"
    )
    grpc_port = os.environ.get("AGY_TEST_GRPC_PORT", "60074")
    rewind_step = os.environ.get("AGY_TEST_REWIND_STEP")
    # Renamed from FAKE_AGY_AUTH_FAILURE / FAKE_AGY_SEND_FAILURE because
    # the adapter env scrub redacts any env name containing "auth" — the
    # test signal would never reach this subprocess otherwise.
    inject_auth_failure = os.environ.get("AGY_TEST_INJECT_HANG") == "1"
    inject_send_failure = os.environ.get("AGY_TEST_INJECT_SENDFAIL")
    exit_code = int(os.environ.get("AGY_TEST_EXIT", "0") or 0)
    reply = os.environ.get("AGY_TEST_REPLY", f"rich-reply: {prompt}")

    if not args.log_file:
        # Behave like real agy: still write the reply.
        sys.stdout.write(reply + "\n")
        return exit_code

    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", buffering=1) as fp:
        _write_line(
            fp,
            f'I0520 12:34:56.000100  1234 sidecar.go:1] '
            f"Language server listening on random port at {grpc_port} for HTTPS (gRPC)",
        )
        time.sleep(0.02)
        if args.conversation:
            _write_line(
                fp,
                f"I0520 12:34:56.000200  1234 print.go:10] "
                f"Print mode: resuming conversation {convo}",
            )
        else:
            _write_line(
                fp,
                "I0520 12:34:56.000200  1234 print.go:10] "
                "Starting new conversation (agent=false)",
            )
            _write_line(
                fp,
                f"I0520 12:34:56.000300  1234 conv.go:20] "
                f"Created conversation {convo}",
            )
        _write_line(
            fp,
            f'I0520 12:34:56.000400  1234 print.go:55] '
            f'Print mode: starting (promptLength={len(prompt)}, model="gemini-3-pro-preview-12-2025", conversationID="{convo}")',
        )
        time.sleep(0.02)
        _write_line(
            fp,
            "I0520 12:34:56.000500  1234 flush.go:1] "
            "Auto-flush: sending 1 queued input(s) (combinedLength=42, media=0)",
        )
        _write_line(
            fp,
            f"I0520 12:34:56.000600  1234 stream.go:1] "
            f"Starting conversation update stream for {convo}",
        )
        if rewind_step:
            _write_line(
                fp,
                f"I0520 12:34:56.000700  1234 rewind.go:1] "
                f"Rewinding conversation {convo} to step {rewind_step}",
            )
        if inject_auth_failure:
            _write_line(
                fp,
                "E0520 12:34:56.000800  1234 auth.go:1] Print mode: auth timed out",
            )
        if inject_send_failure:
            _write_line(
                fp,
                f"E0520 12:34:56.000900  1234 send.go:1] "
                f"Print mode: SendUserMessage failed: {inject_send_failure}",
            )
        time.sleep(0.02)
        _write_line(
            fp,
            "I0520 12:34:57.000000  1234 conv.go:99] Stopping conversation stream",
        )

    sys.stdout.write(reply + "\n")
    return exit_code


if __name__ == "__main__":  # pragma: no cover - executed via subprocess
    raise SystemExit(main())
