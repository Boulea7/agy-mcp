"""Fake `gemini` CLI that emits NDJSON on stdout (`-o stream-json`).

Used by tests that exercise GeminiCliBackend's stream-json parser. The
emitted events deliberately use mixed field aliases (``type`` vs ``kind``,
``session_id`` vs ``thread_id``, ``text`` vs ``content``) so the parser's
alias tolerance is verified.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


HELP_TEXT = """\
gemini [flags]

Flags:
  -p, --prompt         Prompt text.
  -o, --output-format  Output format: text | stream-json
  --sandbox            Enable sandbox.
  --resume             Resume existing thread.
  --model              Model id.
  --help               Show help.
  --version            Print version.
"""


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("-p", "--prompt", dest="prompt", default=None)
    parser.add_argument("-o", "--output-format", dest="fmt", default="text")
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--resume", dest="resume", default=None)
    parser.add_argument("--model", dest="model", default=None)
    parser.add_argument("positional", nargs="*")
    args = parser.parse_args(argv)

    if args.help:
        sys.stdout.write(HELP_TEXT)
        return 0
    if args.version:
        sys.stdout.write("0.42.7-fake\n")
        return 0

    prompt = args.prompt or (args.positional[0] if args.positional else "")
    session = args.resume or os.environ.get("GEMINI_TEST_SESSION", "thread-42")
    reply = os.environ.get("GEMINI_TEST_REPLY", f"gemini reply to: {prompt}")
    inject_error = os.environ.get("GEMINI_TEST_ERROR")
    inject_garbage = os.environ.get("GEMINI_TEST_GARBAGE") == "1"
    exit_code = int(os.environ.get("GEMINI_TEST_EXIT", "0") or 0)

    if args.fmt != "stream-json":
        # Non-stream mode: dump as a single text line (rare path).
        sys.stdout.write(reply + "\n")
        return exit_code

    # Event 1: thread started — uses ``kind`` instead of ``type`` to exercise
    # alias parsing.
    _emit({
        "kind": "thread.started",
        "thread_id": session,
        "model": args.model or "gemini-3-pro",
    })

    # Event 2: a user input echo (uses ``role``+``content`` form).
    _emit({
        "type": "message",
        "role": "user",
        "session_id": session,
        "content": prompt,
    })

    # Optionally inject a malformed line in the middle to verify the parser
    # surfaces a ``stream_decode_failure`` without dropping subsequent events.
    if inject_garbage:
        sys.stdout.write("not-json-at-all\n")
        sys.stdout.flush()

    time.sleep(0.01)

    # Event 3: assistant message.
    _emit({
        "type": "message",
        "role": "assistant",
        "session_id": session,
        "text": reply,
    })

    if inject_error:
        _emit({
            "type": "error",
            "session_id": session,
            "message": inject_error,
        })
        return exit_code

    # Event 4: completion signal — uses ``event`` field, the third alias.
    _emit({
        "event": "turn.completed",
        "thread_id": session,
    })
    hang_after_completed = float(os.environ.get("GEMINI_TEST_HANG_AFTER_COMPLETED", "0") or 0)
    if hang_after_completed > 0:
        time.sleep(hang_after_completed)

    return exit_code


if __name__ == "__main__":  # pragma: no cover - executed via subprocess
    raise SystemExit(main())
