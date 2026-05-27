---
name: collaborating-with-antigravity
description: Delegate analytical, sandboxed, or long-running work to the Google Antigravity (agy) CLI via a JSON-bridge wrapper. Use when you need a second opinion, sandboxed execution, or a detached long-running agent loop. Install location is .agents/skills/collaborating-with-antigravity/.
---

# Collaborating with Antigravity (Codex edition)

`agy-bridge` is a thin JSON wrapper around the Google Antigravity (`agy`)
CLI plus a `gemini` CLI fallback. The bridge returns stable
`BridgeResponse` envelopes designed to drop cleanly into Codex's
exec-json protocol via `--output-protocol codex`.

## When to use

- You want a **second opinion** from Antigravity / Gemini on a tricky
  bug, design call, or code review.
- You want to **prototype** a change in an isolated worktree before
  touching the main checkout.
- You want a **long-running agent loop** to run in the background while
  Codex continues other work.

Avoid for trivial single-step questions — the round-trip is overkill.

## Quick start

```bash
python scripts/agy_bridge.py \
  --cd "/path/to/project" \
  --PROMPT "Find every place that calls db.commit() without a try/except." \
  --mode review \
  --output-protocol codex
```

The bridge prints one JSON line on stdout: `{"success": true,
"SESSION_ID": "…", "agent_messages": "…", "adapter": {…}}`. With
`--output-protocol codex` the event log conforms to Codex exec-json
(`thread.started`, `item.completed`, `turn.completed`).

## Modes

| Mode | Use it for | Worktree | Writes |
|------|-----------|----------|--------|
| `ask` (default) | Q&A, code reading | no | no |
| `plan` | Multi-step planning | no | no |
| `prototype` | Diff-only suggestions | optional | no |
| `review` | Critique a staged change | no | no |
| `execute` | Apply edits in a worktree | yes | requires `--allow-write` |
| `browser` | Research with browsing | no | no |
| `long` | Detached agent loop | no | varies |

## Multi-turn

Capture and reuse `SESSION_ID`:

```bash
# Turn 1
python scripts/agy_bridge.py --cd /proj --PROMPT "Find race conditions in src/queue/"
# → {"SESSION_ID": "abc-123", ...}

# Turn 2
python scripts/agy_bridge.py --cd /proj --SESSION_ID abc-123 \
  --PROMPT "Propose a minimal fix for the worst one."
```

## Detached long jobs

Codex projects that run long agent loops should prefer the MCP tool
surface (`agy_start` / `agy_status` / `agy_result` / `agy_read` /
`agy_cancel` / `agy_sessions`) over polling the CLI in a shell loop. The supervisor
handles worker thread lifecycle, log spooling, and cross-platform
process-group cleanup.

## Output protocols

- `--output-protocol codex` — Codex-shaped exec-json events
  (`thread.started`, `item.completed`, `turn.completed`).
- `--output-protocol claude` — Claude Code stream-json (default).
- `--output-protocol raw` — internal canonical event envelope.

## Safety

The bridge scrubs secrets from every error / log / response, runs
under `SafetyPolicy`, and refuses destructive prompts even with
`--allow-write`. The doctor (`agy_doctor` MCP tool, or
`python -m agy_mcp.doctor`) reports the environment without leaking
secrets.

## Detailed references

- `references/usage.md` — full CLI flag reference + MCP tool surface +
  exit codes.
- `references/prompt-patterns.md` — proven prompt scaffolds per mode.
- `references/security.md` — threat model, secret scrub, denylist,
  worktree, audit log layout.
