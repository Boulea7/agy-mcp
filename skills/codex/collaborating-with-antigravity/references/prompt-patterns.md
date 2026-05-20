# Prompt patterns

Proven scaffolds for each mode. Keep prompts in **English** for the
underlying CLI; translate the user's request only at the boundary.

## ask — explain, locate, summarise

Use when you need a focused single-turn answer.

```
Task: <one-sentence ask>
Repo path: <absolute path>
Constraints:
- Read-only; do not propose changes.
- Cite file paths + line numbers for every claim.
- If you need to ask a clarifying question, ask exactly one.
Deliverable: <what you want back, e.g. "bullet list">
```

## plan — multi-step decomposition

Use before a non-trivial refactor or a multi-file change.

```
Goal: <one-sentence goal>
Inputs:
- Project: <root path>
- Pinned files: <list of paths>
- Known constraints: <test suite to keep green, public API to preserve, …>
Plan format: numbered steps, each step independent, each step <= 30 min of work.
Out of scope: <list anti-goals so we don't get bonus work>
```

## prototype — diff-only

Use when you want code suggestions without committing them.

```
Implement: <change description>
Repo: <root>
Output: unified diff against HEAD; do not write to disk.
Constraints:
- Touch only <files / dirs>.
- Keep <X> backwards-compatible.
- Follow existing style; no formatter pass.
```

Pair with `--mode prototype` and (if you also want a worktree) the
default behaviour is enough — no `--allow-write`.

## review — staged-change critique

Use after producing a diff (your own or Antigravity's) to get a second
opinion.

```
Review: <diff path or paste>
Focus on, in order:
1. correctness (logic bugs, edge cases),
2. security (input validation, secret handling),
3. style (only flag if it hurts readability).
Output: numbered list, each item severity P0/P1/P2/P3, with file:line.
Skip nits.
```

## execute — apply changes in an isolated worktree

Use only when you've already reviewed a prototype and want it applied.

```
Apply: <previously-reviewed diff or change description>
Workspace: <root>
Constraints:
- Run inside a worktree; do not touch the main checkout.
- Run <command, e.g. `pytest tests/test_xxx.py`> after applying.
- If tests fail, revert and report the failure; do not "fix and continue".
Deliverable: list of touched files + test exit code + worktree path.
```

`--mode execute --allow-write` is required. Always pair with a worktree
(default-on) so a misfire doesn't dirty the main checkout.

## long — multi-hour agent loop

Use only when the work genuinely exceeds your turn budget. Always
detach.

```
Mission: <one-paragraph mission statement>
Scope: <files in / out>
Stopping condition: <explicit goal you check via agy_status>
Checkpoint format: every N steps, write a one-line progress note to
the event stream so the supervisor can surface it via agy_read.
On failure: emit an `error` event with a single sentence describing
what stopped you; do not keep retrying.
```

Drive via the MCP tools, not the synchronous CLI:

```python
start = agy_start(PROMPT=long_prompt, cd=root, mode="long", timeout=14400)
# Poll agy_status / agy_read periodically; agy_cancel if you need to abort.
```

## Anti-patterns

- **Do not concatenate unrelated tasks** into one prompt. Antigravity
  performs best when the goal is a single sentence.
- **Do not assume the model is stateful across CLI invocations** unless
  you pass `SESSION_ID`.
- **Do not loop on identical prompts** if the first response was a
  failure — read the `error` field and adjust before retrying.
- **Do not pass `--debug` or `--return-all-messages` in production
  loops** unless you actually consume the extra payload; both increase
  the response size meaningfully.
