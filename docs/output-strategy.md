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

Each row maps a `agy --log-file` (klog) line to the
`CanonicalEvent` shape the adapter emits. The source of truth is the
`_RE_*` table at `src/agy_mcp/adapters/agy.py:75-101`; if you change
a regex, update this table.

```
Language server listening on random port at NNN for HTTPS
  -> {type:"system", subtype:"sidecar_ready",
      metadata:{grpc_port:N, raw:<msg>}}
Language server listening on random port at NNN for HTTP
  -> {type:"system", subtype:"sidecar_http_ready",
      metadata:{http_port:N, raw:<msg>}}
Created conversation <uuid>
  -> {type:"system", subtype:"conversation_started",
      session_id:<uuid>, metadata:{raw:<msg>}}
Print mode: resuming conversation <uuid>
  -> {type:"system", subtype:"conversation_resumed",
      session_id:<uuid>, metadata:{raw:<msg>}}
Print mode: starting (promptLength=N, model=M, conversationID=<uuid>)
  -> {type:"system", subtype:"print_starting",
      session_id:<uuid>,
      metadata:{prompt_length:N, model:M, fields:{...}}}
Starting new conversation (agent=true|false)
  -> {type:"system", subtype:"turn_start",
      metadata:{agent_mode:<bool>}}
Auto-flush: sending N queued input(s) (combined K chars, M media)
  -> {type:"user", subtype:"input_flush",
      metadata:{input_count:N, combined_chars:K, media:M}}
Print mode: SendUserMessage failed: <detail>
  -> {type:"error", subtype:"send_user_message_failed",
      text:<redacted detail>}
Print mode: auth timed out
  -> {type:"error", subtype:"auth_timeout",
      text:"Antigravity OAuth flow timed out; run `agy` once..."}
Print mode: auth error: <detail>
  -> {type:"error", subtype:"auth_error",
      text:<redacted detail>}
Rewinding conversation <uuid> to step N
  -> {type:"system", subtype:"rewind",
      metadata:{step:N}}
Starting conversation update stream for <uuid>
  -> {type:"system", subtype:"stream_start",
      session_id:<uuid>}
Stopping conversation stream
  -> {type:"system", subtype:"turn_end",
      metadata:{raw:<msg>}}
Language server shutting down
  -> {type:"system", subtype:"turn_end",
      metadata:{raw:<msg>}}
```

Notes:

- The session ID extracted from `Created conversation <uuid>` is the
  canonical `SESSION_ID` returned to callers; `--continue` and
  `--conversation <id>` both round-trip through it. The same field is
  populated by `conversation_resumed`, `print_starting`, and
  `stream_start` so any of those is sufficient to anchor downstream
  events to a conversation.
- `_RE_TURN_END` collapses both "Stopping conversation stream" and
  "Language server shutting down" into a single `turn_end` event;
  there is no separate `shutdown` subtype.
- The HTTPS / HTTP sidecar lines emit DIFFERENT subtypes
  (`sidecar_ready` vs `sidecar_http_ready`) so consumers can
  distinguish the gRPC and HTTP listeners without re-parsing the
  raw message.
- Error events use the `text` field for the redacted detail; the
  `subtype` itself carries the failure class. No `source` field is
  emitted on klog-derived events.

### What we deliberately do NOT parse

- **SQLite tail (Strategy B).** `~/.gemini/antigravity-cli/conversations/`
  is empty on disk; the per-conversation `brain/<uuid>/*.pb` files are
  protobuf and appear to be keyring-encrypted. Dead end.
- **gRPC sidecar interception.** `agy` spawns a Language Server on a
  random high port (TLS-wrapped, unpublished proto). Experimental; flagged
  for a future "Lab" mode.
- **Interactive `agy` auth.** Out of scope: the user runs `agy` once in a
  shell and completes the browser/login flow; we only detect local auth
  evidence via a regular legacy OAuth file or a recent keyring-auth success
  line in the CLI log.

## Backend routing

```python
backend = caller.backend          # auto | agy | gemini
if backend == "auto":
    if agy_available_and_authenticated: agy
    elif gemini_available: gemini
    else: agy with explicit capability warnings
```

`gemini` is a true compatibility backend — `gemini-cli` v0.42+ still
ships `--output-format stream-json` and shares Google OAuth with `agy`,
so callers who need real token-level streaming can opt in even when the
project is otherwise agy-first. Auto routing does not choose Gemini just
because streaming would be nicer; pass `backend="gemini"` when real
stream-json output is required.

## Canonical event schema

Pydantic v2 model in `src/agy_mcp/models.py::CanonicalEvent`. Uses
`extra="allow"` so a future agy build that adds new event fields cannot
break parsing.

```python
class CanonicalEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal[
        "system", "user", "assistant",
        "tool_use", "tool_result",
        "result", "error", "subagent_event",
    ]
    subtype: str | None = None
    session_id: str | None = None
    role: str | None = None
    text: str | None = None                       # primary payload text
    content: list[dict[str, Any]] | None = None   # rich content blocks
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] | None = None             # untranslated source
    ts: str = Field(default_factory=_iso_now)     # ISO 8601 UTC
```

`metadata` is the typed escape hatch: structured fields the klog
parser pulls out (`prompt_length`, `input_count`, `grpc_port`, …)
land here instead of bloating the top-level schema. `raw` is reserved
for the verbatim event a non-agy backend (e.g. gemini-cli stream-json)
emitted so callers can reconstruct the original payload if they want.

A typical successful one-shot `ask` produces:

```jsonl
{"type":"system","subtype":"init","session_id":null,"metadata":{"backend":"agy","bin_path":"...","version":"1.0.0","model":"...","cwd":".","mode":"ask","sandbox":false,"capabilities":{"streaming":false,"tool_use":false,"resume":true,"log_file":true,"sandbox":true}}}
{"type":"system","subtype":"sidecar_ready","metadata":{"grpc_port":60074,"raw":"..."}}
{"type":"system","subtype":"conversation_started","session_id":"<uuid>","metadata":{"raw":"..."}}
{"type":"system","subtype":"print_starting","session_id":"<uuid>","metadata":{"prompt_length":42,"model":"...","fields":{...}}}
{"type":"user","subtype":"input_flush","metadata":{"input_count":1,"combined_chars":42,"media":0}}
{"type":"assistant","subtype":"text","session_id":"<uuid>","role":"assistant","text":"<full reply>","content":[{"type":"text","text":"<full reply>"}]}
{"type":"system","subtype":"turn_end","metadata":{"raw":"..."}}
{"type":"result","subtype":"success","session_id":"<uuid>","metadata":{"duration_ms":12345,"exit_code":0,"conversation_id":"<uuid>"}}
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
uses the same `...[truncated]...` middle marker as stdout/stderr tails
and adds a warning to the response. Full event persistence applies to
detached `agy_start` jobs, where the supervisor writes `events.jsonl`
and `agy_read(job_id, translate="raw")` returns the canonical record.
Synchronous `agy` calls use an ephemeral spool and return no `job_id`;
raise `max_output_chars` or request `return_all_messages` when the sync
caller needs more context.

## Fail-fast on missing auth

Older `agy` builds used `~/.gemini/oauth_creds.json`; current official
docs describe system-keyring auth with browser Google Sign-In fallback.
The bridge treats a regular OAuth file or a recent keyring-auth success
line in `~/.gemini/antigravity-cli/log/cli-*.log` as authenticated.
If the OAuth path exists but is a symlink or non-regular file, that is
reported as unsafe and does not fall back to keyring-log inference.

Without this preflight, `agy --print` can hang for the full
`--print-timeout` before failing. The bridge and supervisor short-circuit
non-dry-run agy invocations before spawning the CLI with
`backend='agy' is not authenticated; run agy once and log in.`.

## Future work

Tracked internally outside the release documentation:

- True token-level streaming via the gRPC sidecar (Lab mode).
- `transcript.jsonl` NDJSON watcher promoted from "optional" to default
  once subagent events stabilise.
- Per-turn event flushing for long jobs (today, `agy_read` only returns
  events as they accumulate on disk — there is no SSE / push surface).
