# Project AGENTS.md snippet — Antigravity collaboration (Codex edition)

Drop this into your project's `AGENTS.md` to teach OpenAI Codex when and
how to delegate work to the Antigravity (`agy`) CLI via the `agy-mcp`
bridge.

```markdown
## Antigravity collaboration

Requires `agy-mcp` registered as an MCP server (see the project
README for the Codex `mcp_servers.agy` TOML snippet). Once
registered, this project ships an MCP bridge to Google Antigravity
(the `agy` CLI) via `agy-mcp`. Use it when you need:

- a **second opinion** on a debugging hypothesis or design call
  (`agy --mode review --output-protocol codex`),
- a **sandboxed prototype** before touching the main checkout
  (auto-worktree on `--mode execute --allow-write`),
- a **detached long-running agent loop** that you can poll while you
  continue handling user turns (`agy_start` / `agy_status` /
  `agy_read` / `agy_cancel`).

Available MCP tools: `agy`, `agy_start`, `agy_continue`, `agy_status`,
`agy_read`, `agy_cancel`, `agy_sessions`, `agy_doctor`,
`agy_install_skill`, `agy_purge`.

### Tool routing

| Task type | Path |
|-----------|------|
| Q&A you can answer from the context window | Answer yourself |
| Locate code across a large repo | Use Codex Code Search; only call `agy` if you need a second model |
| Second opinion on a bug | `agy(PROMPT="…", mode="review", output_protocol="codex")` |
| Generate a diff for review | `agy(PROMPT="…", mode="prototype")` (no `--allow-write`) |
| Apply a reviewed diff | `agy(PROMPT="…", mode="execute", allow_write=True)` (auto worktree) |
| Multi-hour refactor | `agy_start(..., mode="long")` then poll with `agy_status` / `agy_read` |

### Output protocol

Always pass `output_protocol="codex"` so the bridge emits
`thread.started` / `item.completed` / `turn.completed` events that
Codex's exec-json parser handles directly. The default is `claude`,
which works but adds an event-shape conversion step.

### Multi-turn

Capture `SESSION_ID` from the first response and pass it back via
`agy_continue(SESSION_ID, PROMPT, ...)`. Antigravity holds the
conversation state; Codex does not need to replay history.

### Safety floor

- The bridge scrubs secrets (`SafetyPolicy.redact`) from every error,
  log, and response field. Path components like `/Users/<you>/` become
  `~/`.
- `allow_write=True` is required for any mutation; the safety policy
  refuses destructive prompts even with the flag.
- `mode="execute"` reads from `~/.ssh`, `~/.aws`, browser cookies, or
  OS keychains will be denied.

### Doctor

Run `agy_doctor()` if Antigravity stops responding or auth looks
broken; it reports the resolved binary, version, auth presence, and
capability matrix. Pass `force_refresh=True` after upgrading `agy`
in place.

### Install order

The collaboration skill installs into the Codex skill directory at
`<project>/.agents/skills/collaborating-with-antigravity/` (via
`agy_install_skill(targets=["codex"], scope="project",
project_root=…)`) or `~/.agents/skills/...` (`scope="user"`). The
SKILL.md tells Codex how to call `agy-bridge`; this AGENTS.md tells
Codex when.
```

Adjust the section heading and project-specific guidance, then commit
`AGENTS.md` to the repo root.
