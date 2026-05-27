---
name: agy-collaboration
description: Collaboration etiquette for Antigravity (agy) sessions invoked via the agy-mcp bridge. Read this before responding when the workflow tag indicates a multi-agent context (Claude / Codex driving via agy-bridge).
---

# Antigravity collaboration playbook

You are being driven by another agent (Claude Code, OpenAI Codex, or
the agy-mcp bridge CLI) for a specific task. The bridge wraps your
stdout into a JSON envelope, so the rules below keep that envelope
useful.

## What the bridge expects from you

1. **Single coherent response per turn.** The bridge buffers your
   stdout until your turn ends, then emits one assistant message.
   Multi-section narration is fine; multi-turn chatter is not.

2. **Plain prose, no emoji.** The bridge runs `SafetyPolicy.redact`
   over your output. Emojis survive but ruin grep / diff workflows
   for the driving agent. Keep prose plain.

3. **Cite file paths and line numbers when you reference code.** The
   driving agent uses `file:line` to jump into the source. `src/foo.py:42`
   is good; "the auth file" is not.

4. **End with a decision or a question, not "let me know if you want me
   to continue".** The bridge's `SESSION_ID` survives; the driving agent
   will pass it back if it wants more. Don't ask permission.

## When the driving agent is in `ask` mode

- You are read-only. Do not propose edits. Do not call tools that
  write to disk. Cite paths and answer the question.
- If you need clarification, ask exactly **one** question. The driving
  agent has limited budget for clarifying round-trips.

## When the driving agent is in `plan` mode

- Produce **numbered, independent, ~30-min steps**. Each step should be
  testable on its own.
- Out-of-scope items belong in an "Out of scope" section, not in the
  plan steps themselves.
- Do **not** estimate hours globally. The driving agent will choose
  pace.

## When the driving agent is in `prototype` mode

- Output a **unified diff** against HEAD. Do not write to disk.
- Keep the diff to the requested files only. Do not opportunistically
  reformat or "improve" surrounding code.

## When the driving agent is in `review` mode

- Output a numbered list. Each item: `[severity] file:line — sentence`.
- Severity ladder: `P0` (ship blocker), `P1` (must-fix this round), `P2`
  (fix if cheap), `P3` (note for later).
- **Skip nits.** Style issues that don't hurt readability are not
  worth flagging.

## When the driving agent is in `execute` mode

- You are running inside a **git worktree** (the bridge auto-creates
  one for `--allow-write` runs). The worktree remains after the run;
  the driving agent will diff it against main and remove it after review.
- If a test fails after you apply a change, **revert and report**.
  Do not "fix and continue" without permission.
- Touch only the files the prompt names.

## When the driving agent is in `long` mode

- You are running detached. The driving agent polls the bridge's
  supervisor, fetches final output with `agy_result`, and reads events
  out of `agy_read`.
- Emit a one-line progress note every N significant steps so the
  supervisor surfaces something useful.
- If you hit an unrecoverable error, emit a single-sentence error
  event and stop. **Do not retry** the same operation more than 3
  times.

## Multi-turn within a session

- Your `SESSION_ID` is stable across turns. The driving agent
  references prior turns by id.
- Conversation history is yours to use, but assume the driving agent
  re-summarises in each prompt. Don't insist the driving agent
  "remembers" — they may have summarised your earlier reply.

## What NOT to do

- **Do not call out to other agents** from inside your reply. You are
  one node in the graph; the driving agent orchestrates.
- **Do not assume PATH / cwd / env beyond what the prompt says.** The
  bridge scrubs env vars and may have moved you into a worktree.
- **Do not refuse legitimate tasks because of mode.** If you think a
  task exceeds the current mode, say so in one sentence and let the
  driving agent re-issue with a different mode.

## References

- `references/collaboration.md` — extended examples of the patterns
  above, common failure modes, and the bridge's structured-failure
  envelope format.
