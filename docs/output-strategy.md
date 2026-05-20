# Output strategy: Hybrid backend + protocol translation

`agy` v1.0.0 does **not** emit structured stream output (no `--json`,
`--output stream-json`, etc.). Its stdout is plain text / markdown, and
the only structured signal is the `--log-file` klog (a Google `klog`
operations log). `agy-mcp` therefore synthesises a canonical event
stream from three independent readers, then translates to whichever
wire format the caller wants.

This document explains the Hybrid backend design and the canonical event
schema. Live `agy --help` output and confirmed capability detection
live in [`cli-capabilities.md`](cli-capabilities.md).

## Three-way reader

For every `agy --print` invocation the adapter spawns:

```
   stdout (plain text)            klog file (--log-file)        ~/.gemini/antigravity-cli/log/**/*.jsonl
        |                                  |                                       |
        v                                  v                                       v
  StdoutBuffer                  KlogTailReader                       TranscriptWatcher (optional)
  - buffers all chunks          - regex-matches lifecycle           - tails NDJSON transcripts
  - emits one "assistant"         events line by line                  written by subagent runs
    event on process exit        - emits real-time events as          - emits each line as a
                                   they appear in the log               "subagent_event"
        \____________________________ events sink ____________________________/
                                      |
                                      v
                          CanonicalEvent stream
```

### What the klog tail recognises

```
Created conversation <uuid>         -> {type: system, subtype: conversation_started, id}
Streaming conversation <uuid>       -> {type: system, subtype: conversation_streaming}
Starting new conversation (agent=*) -> {type: system, subtype: turn_start, is_agent}
Print mode: starting (...)          -> {type: system, subtype: print_started, prompt_length, model}
Auto-flush: sending N queued        -> {type: user, subtype: input, count: N}
Language server listening ... :NNN  -> {type: system, subtype: sidecar_ready, grpc_port: N}
Stopping conversation stream        -> {type: system, subtype: turn_end}
Language server shutting down       -> {type: system, subtype: shutdown}
SendUserMessage failed: <reason>    -> {type: error, source: send, detail}
Print mode: auth ... failed         -> {type: error, source: auth, detail}
auth timed out                      -> {type: error, source: auth, detail: "timed out"}
```

The session ID extracted from `Created conversation <uuid>` is the
canonical `SESSION_ID` returned to callers; `--continue` and
`--conversation <id>` both round-trip through it.

### What we deliberately do NOT parse

- **SQLite tail (Strategy B).** `~/.gemini/antigravity-cli/conversations/`
  is empty on disk; the per-conversation `brain/<uuid>/*.pb` files are
  protobuf and appear to be keyring-encrypted. Dead end.
- **gRPC sidecar interception.** `agy` spawns a Language Server on a
  random high port (TLS-wrapped, unpublished proto). Experimental; flagged
  for a future "Lab" mode.
- **`agy login` / interactive auth.** Out of scope: the user runs it once
  in a shell; we only detect whether `~/.gemini/oauth_creds.json` exists.

## Backend routing

```python
backend = caller.backend          # auto | agy | gemini
if backend == "auto":
    if needs_sandbox or wants_agentic_execution: agy
    elif needs_structured_streaming and gemini_available: gemini
    else: agy with degraded events
```

`gemini` is a true compatibility backend — `gemini-cli` v0.42+ still
ships `--output-format stream-json` and shares Google OAuth with `agy`,
so callers who need real token-level streaming can opt in even when the
project is otherwise agy-first.

## Canonical event schema

Pydantic v2 model in `src/agy_mcp/models.py::CanonicalEvent`. Uses
`extra="allow"` so a future agy build that adds new event fields cannot
break parsing.

```python
class CanonicalEvent(BaseModel):
    type: Literal["system", "user", "assistant", "error", "result", "subagent"]
    subtype: str | None = None
    session_id: str | None = None
    timestamp: str | None = None          # ISO 8601 UTC
    message: dict | None = None
    detail: str | None = None
    source: str | None = None             # for type=error
    duration_ms: int | None = None
    exit_code: int | None = None
    artifacts: list[dict] | None = None
```

A typical successful one-shot `ask` produces:

```jsonl
{"type":"system","subtype":"init","backend":"agy","model":"...","capabilities":{...}}
{"type":"system","subtype":"sidecar_ready","grpc_port":60074}
{"type":"system","subtype":"conversation_started","id":"<uuid>"}
{"type":"system","subtype":"print_started","prompt_length":42,"model":"..."}
{"type":"user","subtype":"input","count":1}
{"type":"assistant","message":{"content":[{"type":"text","text":"<full reply>"}]}}
{"type":"system","subtype":"turn_end"}
{"type":"result","subtype":"success","duration_ms":12345,"exit_code":0,"session_id":"<uuid>"}
```

## Protocol translator (`protocol.py`)

```python
output_protocol: Literal["raw", "claude", "codex"] = "claude"
```

### `raw`

Canonical envelope passes through unchanged. Use this when integrating
into a tool that wants the highest-fidelity stream.

### `claude` (default)

Maps to Claude Code's stream-json shape so existing claude-side parsers
work without changes:

```jsonl
{"type":"system","subtype":"init","session_id":"<uuid>","model":"...","tools":[]}
{"type":"assistant","message":{"id":"...","role":"assistant","content":[{"type":"text","text":"<reply>"}]},"session_id":"<uuid>"}
{"type":"result","subtype":"success","session_id":"<uuid>","duration_ms":12345}
```

### `codex`

Maps to Codex exec-json events (`thread.started`, `item.completed`,
`turn.completed`):

```jsonl
{"type":"thread.started","thread_id":"<uuid>"}
{"type":"item.completed","item":{"type":"agent_message","text":"<reply>"}}
{"type":"turn.completed","turn":{"status":"completed","duration_ms":12345}}
```

The translator is stateless — it consumes one canonical event at a
time and emits zero, one, or many translated events depending on the
target protocol.

## Per-call truncation

`max_output_chars` (default `60000`) caps the buffered
`agent_messages` field returned in the synchronous envelope. Truncation
is signalled with a `[truncated: N chars omitted]` marker; the full
stream is always preserved in the session store's `events.jsonl` so
`agy_read(job_id, translate="raw")` returns the canonical record.

## Fail-fast on missing OAuth

If `~/.gemini/oauth_creds.json` is absent, `agy --print` will hang for
the full `--print-timeout` (5+ minutes) before failing. The bridge
short-circuits this at request validation: `doctor.run()` checks the
file presence and fails fast with the message
`"Google OAuth credentials missing; run \`agy login\` before any
non-dry-run invocation."`. The doctor also lstat's the credentials file
and warns when it is a symlink or a non-regular file (e.g.
`/dev/zero`).

## Future work

Logged in `docs/review-followups.md`:

- True token-level streaming via the gRPC sidecar (Lab mode).
- `transcript.jsonl` NDJSON watcher promoted from "optional" to default
  once subagent events stabilise.
- Per-turn event flushing for long jobs (today, `agy_read` only returns
  events as they accumulate on disk — there is no SSE / push surface).
