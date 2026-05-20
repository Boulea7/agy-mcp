# Collaboration reference

Extended examples and failure-mode catalogue for the `agy-collaboration`
skill. The driving agent reads your stdout via `agy-bridge` and
translates it into Claude or Codex event protocols; the rules below keep
that translation faithful.

## Reply structure cheat sheet

| Mode | Lead with | Body | End with |
|------|----------|------|----------|
| ask | Direct answer | Citations + reasoning | Decision or one clarifying question |
| plan | Goal restatement | Numbered steps | Out-of-scope list |
| prototype | Diff header | Unified diff | "Tests touched:" line |
| review | Verdict line | Numbered findings | "No further findings." or open question |
| execute | "Worktree: <path>" | Files touched, commands run | Test result + revert summary if needed |
| long | One-line mission echo | Step log | Next-step intention |

## Common failure modes (don't do these)

### "Let me know if you want me to continue."
The driving agent will continue if it wants to — it has `SESSION_ID`.
End on a decision (`Recommend X`), a question (`Should we Y?`), or a
hand-off (`Ready for review`). Never end on a permission ask.

### Wrapping the whole reply in markdown headers
The bridge passes your text through. Light markdown is fine; a 6-level
header tree is noise. Aim for the same density a human reviewer would
read in 30 seconds.

### Citing "the auth file" instead of `src/auth/login.py:42`
The driving agent uses `file:line` to navigate. Without one, your
findings are unactionable.

### Proposing a diff in plan mode
`plan` mode wants strategy. Save the diff for `prototype` mode, which
the driving agent will explicitly switch to.

### Assuming the main checkout changed
`execute` mode runs you inside a retained review worktree. Report the
worktree path and touched files so the driving agent can inspect, merge,
or remove it.

### Reading secrets to "verify"
The safety policy denies reads of `~/.ssh`, `~/.aws`, `~/.gemini/oauth_creds.json`,
browser cookies, OS keychain entries when the driving agent is in
`execute` mode. Refuse politely if the prompt asks.

## Structured-failure envelope (what the driving agent sees on error)

If your turn ends with an error, the bridge emits:

```json
{
  "success": false,
  "SESSION_ID": "abc-123",
  "error": "<redacted, one-line description>",
  "agent_messages": "",
  "cwd": "/proj",
  "adapter": {"backend": "agy", "version": "1.0.0"}
}
```

`error` is single-line and redacted. The driving agent will likely
retry with adjusted prompt; do not assume the second attempt is a
fresh turn — the `SESSION_ID` is the same.

## Long-mode checkpoint format

A good long-mode progress note:

```
Step 3/12 [plan]: reviewed src/auth/*, found 4 callers of db.commit() that need a try/except. Moving to prototype.
```

A bad long-mode progress note:

```
Working on it...
```

The supervisor turns the line into a `subagent_event` the driving
agent reads. Make it actionable.

## When the bridge crashes (you'll know because the next turn opens with `success=false`)

- Don't re-emit your previous output verbatim.
- Don't ask "what happened?" — read the `error` string.
- If the error mentions `oauth_creds.json missing`, the driving agent
  needs to run `agy login`; you can't fix that yourself.
- If the error mentions `request rejected by safety policy`, re-read
  your output for destructive patterns (`rm -rf`, writes to
  `~/.ssh`, etc.) and reduce scope.
