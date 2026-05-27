# Usage reference

This file expands on the `SKILL.md` quick start and covers the full CLI
flag set, the long-job lifecycle, the MCP tool surface, and exit-code
semantics.

## CLI flag reference

```
agy-bridge --PROMPT <text> --cd <dir>
          [--SESSION_ID <id>] [--mode ask|plan|prototype|review|execute|browser|long]
          [--model <name>] [--sandbox] [--allow-write]
          [--worktree default|true|false]
          [--backend auto|agy|gemini]
          [--output-protocol claude|raw|codex]
          [--timeout <seconds>] [--max-output-chars <int>]
          [--return-all-messages]
          [--dry-run] [--debug]
          [--extra-env KEY=value ...]
```

Notable defaults:

- `--timeout` defaults to `900` seconds (15 min). For durable long jobs,
  use the MCP `agy_start` tool rather than CLI `--detach`.
- `--worktree default` (the default) lets config / env decide; pass
  `--worktree true` to force-on or `--worktree false` to force-off.
- `--backend auto` chooses `agy` when available, falling back to `gemini`
  when only `gemini` is on PATH.
- `--output-protocol claude` is best for Claude Code; `codex` for OpenAI
  Codex; `raw` when you want canonical envelopes.
- `--max-output-chars` caps the size of the buffered `agent_messages`
  field (default `60000`); the bridge truncates with a marker rather
  than returning the full buffer.

## Long jobs (start / status / result / read / cancel)

The CLI bridges to an MCP tool surface. The skill should prefer the MCP
tools (`agy_start`, `agy_status`, `agy_result`, `agy_read`, `agy_cancel`,
`agy_sessions`) over polling the CLI because the supervisor handles
worker thread lifecycle, log spooling, and cross-platform process group
cleanup.

```python
# Pseudo-flow:
start = agy_start(PROMPT="big refactor", cd="/proj", mode="long")
job_id = start["job_id"]

# Poll status until completion:
while True:
    st = agy_status(job_id)
    if st["record"]["status"] in {"completed", "failed", "cancelled", "upstream_error"}:
        break

# Fetch the human-readable final output:
result = agy_result(job_id)

# Read events (raw canonical envelope by default):
events = agy_read(job_id)

# Or translated for your protocol:
events = agy_read(job_id, translate="claude")

# Cancel a runaway job:
agy_cancel(job_id)
```

The metadata tools accept a full `job_id` or a unique prefix. For example,
`agy_status("job_177986")` resolves to the matching stored job when exactly
one id starts with that prefix; ambiguous prefixes return `success=false`
with an explicit ambiguity error.

## Response envelope

Every CLI invocation prints a single JSON line on stdout:

```json
{
  "success": true,
  "SESSION_ID": "abc-123",
  "job_id": null,
  "status": "completed",
  "agent_messages": "string or list",
  "all_messages": [],
  "artifacts": [],
  "error": null,
  "warnings": [],
  "cwd": "/proj",
  "adapter": {
    "backend": "agy",
    "bin_path": "/usr/local/bin/agy",
    "version": "1.0.0",
    "model": "...",
    "output_protocol": "claude",
    "supports_streaming": false,
    "supports_tool_events": false
  },
  "command_preview": null,
  "log_path": "/path/to/agy.log",
  "created_at": "2026-05-20T12:34:56Z",
  "updated_at": "2026-05-20T12:35:10Z"
}
```

On failure, `success=false` and `error` is non-null. Always switch on
`success` before consuming other fields.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | success; JSON envelope on stdout |
| `1` | bridge-level failure; envelope still on stdout |
| `2` | argparse/CLI usage error |
| `127` | launcher not found (uvx / python missing) |

`subprocess` callers should read stdout JSON regardless of exit code so
they get the redacted error string.

## Environment overrides

- `AGY_BRIDGE_CMD` — full shell command for the bridge launcher (useful
  for the skill to pin a local checkout in development).
- `AGY_CLI_DISABLE_AUTO_UPDATE=1` — passed through to `agy` to keep
  builds reproducible.
- `AGY_MCP_WORKTREE_DEFAULT=0/1` — overrides the config-file default.
- `AGY_MCP_BACKEND=auto|agy|gemini` — overrides backend selection.
- `AGY_MCP_OUTPUT_PROTOCOL=raw|claude|codex` — overrides the wire format.

Higher precedence flags beat env vars beat config.toml.

## When the bridge fails

`success=false` and `error` will contain a redacted human-readable
sentence. Common categories:

- **`agy/gemini not found on PATH`** — install per
  `https://docs.astral.sh/uv/getting-started/installation/` (uv) then
  `uv tool install --from git+https://github.com/Boulea7/agy-mcp.git`.
- **`Google OAuth credentials missing`** — run `agy` in the user's
  shell once and complete the interactive login flow. The bridge cannot
  do this for you.
- **`request rejected by safety policy`** — the prompt or argv matched
  a destructive pattern. Re-read the prompt; do not just retry.
- **`supervisor busy`** — the concurrent-job cap is reached. Wait for
  an existing job to finish or raise the cap in the MCP server config.
