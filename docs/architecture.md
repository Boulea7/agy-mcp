# Architecture

`agy-mcp` is a layered bridge between an agentic caller (Claude Code,
OpenAI Codex) and Google Antigravity's `agy` CLI. Each layer is in its
own module so a future maintainer can replace one without touching the
others.

```
+----------------------------------------------------------------+
|  Caller (Claude Code / Codex)                                  |
|  - SKILL.md tells the agent WHEN to delegate                   |
|  - Receives a JSON envelope (BridgeResponse) on every call     |
+--------------------------+-------------------------------------+
                           |
                  MCP stdio (FastMCP)
                           |
+--------------------------v-------------------------------------+
|  MCP server  (src/agy_mcp/server.py)                           |
|  - 9 tools: agy, agy_start, agy_continue, agy_status,          |
|    agy_read, agy_cancel, agy_sessions, agy_doctor,             |
|    agy_install_skill                                           |
|  - Singletons: config, safety, session_store, supervisor       |
|  - Async tools wrap a per-loop CapacityLimiter (8 by default)  |
+--------------------------+-------------------------------------+
                           |
            +--------------+----------------+
            |                               |
+-----------v-----------+      +------------v-----------+
| Bridge CLI            |      | Supervisor             |
| (bridge.py)           |      | (supervisor.py)        |
|                       |      |                        |
| - argparse front-end  |      | - Thread per job       |
| - Reads/validates     |      | - Semaphore-bound      |
|   BridgeRequest       |      |   concurrency cap      |
| - Calls adapter.run() |      | - Cross-platform       |
|   synchronously       |      |   process group        |
| - Emits BridgeResponse|      |   cancel (POSIX killpg |
+-----------+-----------+      |   / Windows BREAK)     |
            |                  +-------------+----------+
            |                                |
            +--------------+-----------------+
                           |
+--------------------------v-------------------------------------+
|  Adapter layer                                                 |
|  - base.py  : BaseAdapter, AdapterRunResult, EventSink         |
|  - agy.py   : AgyPrintBackend (Hybrid: stdout + log tail)      |
|  - gemini.py: GeminiCliBackend (compat fallback)               |
|  - protocol.py: ProtocolTranslator (raw / claude / codex)      |
+--------------------------+-------------------------------------+
                           |
                  subprocess (POpen)
                           |
+--------------------------v-------------------------------------+
|  External binary                                               |
|  - agy (Google Antigravity CLI)                                |
|  - or gemini (fallback when agy is not on PATH)                |
+----------------------------------------------------------------+
```

## Cross-cutting modules

### `safety.py` — SafetyPolicy

Every error string, log message, and response field is run through
`SafetyPolicy.redact()` before leaving the process. The policy:

- Recognises PEM blocks, JWT tokens, AWS access key IDs, Bearer / Authz
  headers, and anonymises absolute home-directory paths (`/Users/<u>/`
  → `~/`, `/home/<u>/` → `~/`, `C:\Users\<u>\` → `~\`).
- Caches compiled patterns; the cache is RLock-guarded so two MCP tool
  invocations cannot race on first use.
- Rejects destructive prompts (`rm -rf`, `mkfs`, `dd if=/dev/zero`,
  pattern-based) even when `allow_write=True`.

### `config.py` — layered config

Resolution precedence (highest wins):

1. Tool / CLI flag argument.
2. Environment variable (`AGY_MCP_WORKTREE_DEFAULT`,
   `AGY_MCP_ALLOW_WRITE_DEFAULT`, `AGY_MCP_BACKEND`,
   `AGY_MCP_OUTPUT_PROTOCOL`).
3. `~/.config/agy-mcp/config.toml` (or `%APPDATA%/agy-mcp/config.toml`).
4. Built-in defaults.

`AGY_CLI_DISABLE_AUTO_UPDATE` is **not** part of this resolution chain
— it is a subprocess passthrough set by `adapters/agy.py` into the
child's environment so the `agy` CLI does not phone home during a
build, and it has no effect on `agy-mcp`'s own behaviour.

Defaults: `worktree=True` for `execute` mode, `allow_write=False`,
`backend="auto"`, `output_protocol="claude"`.

### `session_store.py` — per-job filesystem layout

```
<root>/<job_id>/
  meta.json          # JobRecord (status, timing, redacted command)
  events.jsonl       # CanonicalEvent stream (one JSON object per line)
  stdout.log         # raw agy stdout
  stderr.log         # raw agy stderr
  agy.log            # agy --log-file (klog) — lifecycle events
  artifacts/         # files emitted by execute mode
```

`<root>` defaults to `~/.agy-mcp/sessions/` and is `mkdir(0o700)` on
first use. Every write goes through `safe_write_text(verify_under=root)`
to defeat parent-symlink swaps.

### `worktree.py` — git isolation

When `mode=execute` and `allow_write=True`, the bridge creates a git
worktree at `<repo>/.agy-mcp/worktrees/<session_id>/` and runs `agy`
with `--add-dir <worktree>`. The worktree remains after the run so the
caller can inspect, merge, or discard the branch. Falls back to running
directly in the repo only when the user has `worktree=False` set;
otherwise refuses.

### `utils.py` — `safe_write_text`

The single write primitive used by `session_store`, `install`, and
`worktree`. Walks every parent component from `verify_under` down to
`path.parent` with `is_symlink()` (lstat semantics), `O_NOFOLLOW`s the
tempfile, atomic `os.replace`, re-walks the parents after rename, then
does a final `relative_to(verify_under)` on the resolved path. The
post-walk is a detect-after-the-fact audit signal (an attacker who wins
the race between `mkstemp` and rename has already published the
attacker-controlled leaf; the raise surfaces the breach but cannot undo
it). The `openat`-based airtight fix is logged in
`docs/review-followups.md`.

## Request / response shape

```python
class BridgeRequest(BaseModel):
    prompt: str                          # 1 ≤ len ≤ 256_000
    cwd: str = "."
    session_id: str | None = None
    model: str | None = None
    sandbox: bool = False
    mode: Literal["ask","plan","prototype","review","execute","browser","long"] = "ask"
    return_all_messages: bool = False
    timeout: int = 900                    # 1 ≤ value ≤ 86400 (24h)
    detach: bool = False
    allow_write: bool = False
    worktree: bool | None = None          # None -> use config default
    max_output_chars: int = 60000         # 1 ≤ value ≤ 8 MiB
    backend: Literal["auto", "agy", "gemini"] = "auto"
    output_protocol: Literal["raw", "claude", "codex"] = "claude"
    extra_env: dict[str, str] = {}        # validated: ^[A-Z_][A-Z0-9_]*$,
                                          # value cannot contain \n / \r / NUL,
                                          # max 64 entries, max 4096 chars each
    debug: bool = False

class BridgeResponse(BaseModel):
    success: bool
    SESSION_ID: str = ""
    job_id: str | None = None
    status: Literal["completed","running","failed","cancelled","unknown"] = "unknown"
    agent_messages: str | list[dict] = ""
    all_messages: list[dict] = []
    artifacts: list[dict] = []
    error: str | None = None
    warnings: list[str] = []
    cwd: str = ""
    # Structured metadata: backend, bin_path, version, capability matrix,
    # extra fields (extra="allow" so backends can attach their own keys).
    # See ``src/agy_mcp/models.py::AdapterMetadata``.
    adapter: AdapterMetadata = AdapterMetadata()
    command_preview: list[str] | None = None  # debug/dry-run only
    log_path: str | None = None
    created_at: str = ""
    updated_at: str = ""
```

Both models use Pydantic v2 (`extra="forbid"` on request, `extra="allow"`
on canonical events so future agy event types survive without a schema bump).

## MCP tool surface

| Tool | Sync | Purpose |
|---|---|---|
| `agy` | yes (async-wrapped) | One-shot synchronous call; upstream-reference-compatible arg set + new `mode`/`backend`/`output_protocol`/`worktree`/`allow_write`/`extra_env` |
| `agy_continue` | yes (async-wrapped) | Resume a prior `SESSION_ID` |
| `agy_start` | no | Spawn a background job; returns `job_id` immediately |
| `agy_status` | yes | `running` / `completed` / `failed` / `cancelled` + timing |
| `agy_read` | yes | Stream events (raw or protocol-translated) + artifacts |
| `agy_cancel` | yes | Process-group cancel (POSIX `killpg` / Windows `CTRL_BREAK_EVENT`) |
| `agy_sessions` | yes | List recent jobs with mtime / status / cwd summary |
| `agy_doctor` | yes | Environment + auth probe (no secrets); `force_refresh=True` after CLI upgrade |
| `agy_install_skill` | yes | Install SKILL bundle into Claude / Codex / Antigravity dirs |

Sync tools are declared `async def` so they yield the FastMCP stdio loop
while doing blocking I/O, but they internally run the bridge in a worker
thread via `anyio.to_thread.run_sync`. A per-loop `CapacityLimiter`
caps concurrent calls at 8.

## Failure routing

Every non-trivial code path returns through `_structured_failure` →
`BridgeResponse` so callers always get a `success=False` envelope rather
than an exception. `_structured_failure` runs the error message through
`SafetyPolicy.redact` so paths and tokens are anonymised before they
land in the caller's transcript.

## Related docs

- `docs/installation.md` — how to install and register.
- `docs/output-strategy.md` — adapter / event-synthesis / protocol-translator detail.
- `docs/security.md` — threat model and what is / is not defended.
- `docs/cli-capabilities.md` — what `agy --help` actually reports (refreshed when the CLI is updated).
- `docs/examples.md` — six end-to-end usage scenarios.
- `docs/comparison-with-upstream-reference.md` — what we inherited and what we extended.
