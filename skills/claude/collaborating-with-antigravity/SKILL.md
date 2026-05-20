---
name: collaborating-with-antigravity
description: Delegate analytical, sandboxed, or long-running work to the Google Antigravity (agy) CLI through a JSON-bridge wrapper. Use when you need a second opinion, want to isolate side effects in a worktree, or need to run a long agent loop without blocking the main conversation.
---

# Collaborating with Antigravity

`agy-bridge` is a thin JSON wrapper around the Google Antigravity (`agy`)
CLI plus an optional `gemini` CLI fallback. It produces stable
`BridgeResponse` envelopes so this skill can drive Antigravity sessions
deterministically.

## When to use

- You need a **second opinion** on a debugging hypothesis or a code review.
- You need to **prototype** a change in isolation (auto-worktree).
- You need to **run a long agent loop** (planning, large refactors) without
  blocking your own conversation: start a background job and poll.
- You want a **sandboxed execution** of code Antigravity proposes.

Do not use for trivial one-shot questions — the round-trip overhead is
not worth it. Prefer direct work for those.

## Quick start

```bash
# One-shot synchronous call (ask mode, no write):
python scripts/agy_bridge.py \
  --cd "/path/to/project" \
  --PROMPT "Explain the auth flow in src/auth/"
```

Output is a single JSON line on stdout: `{"success": true, "SESSION_ID":
"…", "agent_messages": "…", "adapter": {…}, …}`.

## Modes

`--mode` controls the agent persona and downstream safety policy:

| Mode | Use it for | Worktree? | Writes? |
|------|-----------|-----------|---------|
| `ask` (default) | Q&A, code reading, design discussion | no | no |
| `plan` | Multi-step planning, breakdown | no | no |
| `prototype` | Generate diffs for review | optional | no |
| `review` | Code review of staged changes | no | no |
| `execute` | Make file edits in the workspace | **yes** | requires `--allow-write` |
| `browser` | Interactive browsing / research | no | no |
| `long` | Multi-hour agent loop, expect to poll status | no | no |

`execute` always creates a worktree by default; combine with
`--allow-write` to opt in to mutations. The worktree default is
configurable in `~/.config/agy-mcp/config.toml` (see references/security.md).

## Multi-turn

Capture `SESSION_ID` from the first response, then pass it back:

```bash
# Turn 1
python scripts/agy_bridge.py --cd "/proj" --PROMPT "Analyse src/auth/"
# → {"SESSION_ID": "abc-123", "agent_messages": "…"}

# Turn 2 (continues the same conversation)
python scripts/agy_bridge.py --cd "/proj" --SESSION_ID abc-123 \
  --PROMPT "Now propose a refactor."
```

## Long jobs (start / status / read / cancel)

For tasks that exceed a single Claude turn, use the supervisor surface
via the MCP tools `agy_start` / `agy_status` / `agy_read` / `agy_cancel`.
See `references/usage.md` for full examples.

## Capability detection

The bridge advertises adapter capabilities in every response under
`"adapter"`. Trust those over your prior assumptions — `agy` does not
stream tokens, so `supports_streaming=false` is normal; `gemini` does,
so the fallback path returns finer-grained events.

## Output protocols

`--output-protocol claude` (default) emits events shaped like Claude
Code stream-json. `--output-protocol codex` emits OpenAI Codex
exec-json. `--output-protocol raw` returns the canonical event envelope
unchanged. Pick the one that matches your downstream parser.

## Safety floor

- Secrets are scrubbed from every response and log via `SafetyPolicy`.
- `--allow-write` is required for any mutation; safety policy denies
  destructive prompts even with the flag.
- All process exits are reported with `exit_code` and `duration_ms`.

## Detailed references

- `references/usage.md` — full CLI flag reference, MCP tool surface,
  long-job patterns, exit codes.
- `references/prompt-patterns.md` — proven prompt scaffolds for `ask`,
  `plan`, `prototype`, `review`, and `execute` modes.
- `references/security.md` — threat model, secret handling, denylist,
  worktree behaviour, audit log layout.
