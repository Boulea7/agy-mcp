# Project CLAUDE.md snippet — Antigravity collaboration

Drop this into your project's `CLAUDE.md` to teach Claude Code when and
how to delegate work to the Antigravity (`agy`) CLI via the `agy-mcp`
bridge.

```markdown
## Antigravity collaboration

Requires `agy-mcp` registered as an MCP server (see the project
README for the `claude mcp add agy …` command). Once registered,
this project ships an MCP bridge to Google Antigravity (the `agy`
CLI) via `agy-mcp`. Use it when you need:

- a second opinion on a debugging hypothesis (`agy --mode review`),
- a sandboxed prototype before touching the main checkout (auto-worktree
  on `--mode execute --allow-write`),
- a long agent loop running detached while you keep responding
  (`agy_start` / `agy_status` / `agy_read` / `agy_cancel`).

Available MCP tools (registered as `agy`, `agy_start`, `agy_continue`,
`agy_status`, `agy_read`, `agy_cancel`, `agy_sessions`, `agy_doctor`,
`agy_install_skill`, `agy_purge`):

- `agy(PROMPT, cd, mode, …)` — synchronous one-shot call.
- `agy_continue(SESSION_ID, PROMPT, cd, …)` — resume a prior session.
- `agy_start(PROMPT, cd, mode="long", …)` — background job, returns
  `job_id`.
- `agy_status(job_id)` / `agy_read(job_id)` / `agy_cancel(job_id)` —
  long-job lifecycle.
- `agy_doctor()` — environment + auth probe (no secrets).
- `agy_install_skill(targets, scope, project_root)` — install the
  collaboration skill into Claude / Codex / Antigravity skill dirs.
- `agy_purge(days)` — drop session-store directories older than `days`
  (refuses `days<=0`).

### When to use

| Task type | Recommended path |
|-----------|-----------------|
| Q&A you can answer from open files | Answer yourself |
| Second opinion on a bug hypothesis | `agy --mode review` |
| Generate code for review | `agy --mode prototype` |
| Apply a previously-reviewed change | `agy --mode execute --allow-write` |
| Multi-hour refactor | `agy_start --mode long` then poll |

### When NOT to use

- Trivial questions you can answer directly. The round-trip is not worth
  it.
- Anything that needs the actual Anthropic / OpenAI conversation state.
  Antigravity is a separate model with its own context.

### Safety floor

- The bridge scrubs secrets (`SafetyPolicy.redact`) from every error /
  log / response. Trust the redaction but assume nothing — never paste
  raw responses into shared channels.
- `--allow-write` is required for any mutation; safety policy denies
  destructive prompts even with the flag.
- The bridge refuses prompts mentioning `~/.ssh`, `~/.aws`, browser
  cookies, OS keychain when `--mode execute` is set.

### Doctor before you trust

If `agy` results look wrong, run `agy_doctor()` first. It reports the
resolved binary path, version, auth presence, and capability matrix
without leaking secrets. Re-run with `force_refresh=true` after
upgrading the `agy` CLI in place.
```

Adjust the section heading and any project-specific guidance, then
commit `CLAUDE.md` to the repo root.
