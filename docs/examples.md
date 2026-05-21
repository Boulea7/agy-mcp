# Examples

Seven end-to-end scenarios showing the typical bridge call patterns. All
examples assume `agy-mcp` is registered with the caller (see
[`installation.md`](installation.md)).

The examples use **MCP tool calls** rather than the raw CLI; the same
arguments work from `agy-bridge` directly.

---

## 1. Second opinion on a bug hypothesis

You suspect a memory leak in a Go service but want a second model to
challenge the theory before you patch.

```python
agy(
    PROMPT="""
The pprof heap profile in /tmp/heap.pprof shows 60MB retained by
*sql.Rows allocations after the request completes. Hypothesis: the
caller in handlers/widget.go never calls rows.Close() on the error
path. Please review handlers/widget.go and widgets/repo.go and tell me
if the hypothesis holds or what I'm missing.
""",
    cd="/Users/me/work/widgetsvc",
    mode="review",
    output_protocol="codex",  # Codex caller; use 'claude' for Claude Code
)
```

Returns synchronously with `agent_messages` containing the review.
`mode="review"` is a caller convention for review-only delegation; the
bridge does not pass a downstream CLI flag that can enforce read-only
behavior inside `agy`.

---

## 2. Sandboxed prototype before touching the main checkout

You want to generate a diff for review without risking the working tree.

```python
result = agy(
    PROMPT="Refactor handlers/widget.go to use sqlx instead of database/sql; preserve test behaviour.",
    cd="/Users/me/work/widgetsvc",
    mode="prototype",   # generates diff in agent_messages, NO --allow-write
)
# result["agent_messages"] now contains a unified diff you can review.
# Nothing was written to disk.
```

To **apply** a reviewed diff, run a second call with
`mode="execute", allow_write=True`. The bridge auto-creates a git
worktree at `.agy-mcp/worktrees/<session_id>/`, runs the change there,
and leaves it for you to inspect / merge.

---

## 3. Long-running refactor (detached)

The change will take 30+ minutes. You don't want to block the caller.

```python
start = agy_start(
    PROMPT="""
Rewrite the legacy auth middleware in src/middleware/auth.py to use
the new TokenStore abstraction. There are ~40 call sites; update them
all and keep tests green. Don't merge until tests pass.
""",
    cd="/Users/me/work/api",
    mode="long",
    # `allow_write=True` is required for execute-mode mutation. Bridge
    # default `worktree=True` means the run lands under
    # `.agy-mcp/worktrees/<session_id>/` rather than the live checkout —
    # that's the safety net. Override with config or
    # `AGY_MCP_WORKTREE_DEFAULT=0` if you intentionally want writes in
    # the main tree.
    allow_write=True,
)
job_id = start["job_id"]
print("kicked off", job_id)

# ... do other work, handle other turns ...

# Poll status when you want a check-in. agy_status returns:
#   {"success": True, "record": {... JobRecord fields incl. artifacts ...}}
status = agy_status(job_id)
record = status["record"]
if record["status"] in {"completed", "failed", "cancelled"}:
    out = agy_read(job_id, translate="claude")
    # out["events"] is the claude-protocol event stream (a list of dicts).
    # out["count"] is len(out["events"]). Artifacts (files written under
    # the worktree) live on the JobRecord, NOT on agy_read's response:
    artifacts = record["artifacts"]
```

If the job hangs you can `agy_cancel(job_id)` — the supervisor sends
`SIGTERM` to the process group, waits a grace window, then `SIGKILL`s
the leftover.

---

## 4. Multi-turn continuation

You want to ask follow-up questions in the same conversation context.

```python
first = agy(
    PROMPT="Explain the design rationale behind the TokenStore abstraction in src/middleware/auth.py.",
    cd="/Users/me/work/api",
    mode="ask",
)
session_id = first["SESSION_ID"]

# Later in the same caller transcript:
second = agy_continue(
    SESSION_ID=session_id,
    PROMPT="Now compare it to the older SessionCookieStore and tell me when each is preferred.",
    cd="/Users/me/work/api",
)
# `agy` holds the conversation state — the caller does NOT need to
# replay history. session_id round-trips between calls so multi-turn
# is just `agy_continue(SESSION_ID, PROMPT, ...)`.
```

---

## 5. Environment health check (no API call)

Before kicking off a complex job, verify the environment is ready
without consuming an `agy` request.

```python
out = agy_doctor()
# agy_doctor returns the envelope:
#   {"success": True, "report": {...}, "version": "<agy-mcp version>"}
# The actual report lives under out["report"]:
report = out["report"]
# report["healthy"] is True iff every check has ok=True OR severity != "error".
# report["checks"] is a list of:
#   {"name": "python",        "ok": True,  "severity": "info",
#    "detail": "detected Python 3.12.13; requires >= 3.11"}
#   {"name": "uv",            "ok": True,  ...}
#   {"name": "agy_binary",    "ok": True,
#    "detail": "agy 1.0.0 at ~/.local/bin/agy"}
#   {"name": "gemini_binary", "ok": True or False, ...}
#       # gemini is an OPTIONAL fallback backend — `agy-mcp` works
#       # without it. ok=False with severity="warning" is normal; only
#       # an explicit `backend="gemini"` call needs the binary, and
#       # then the doctor warning becomes the prerequisite check.
#   {"name": "auth",          "ok": True,
#    "detail": "Google OAuth credentials present at ~/.gemini/oauth_creds.json"}
#   {"name": "session_store", "ok": True,
#    "detail": "session store at ~/.agy-mcp/sessions"}
#
# Some checks emit per-warning extra rows whose name is
# "<label>_warning" (e.g. "agy_warning", "gemini_warning") — these are
# additional diagnostic strings, not separate probes. Filter on `name`
# if you only want the main probes.

# After upgrading the agy CLI in place:
out = agy_doctor(force_refresh=True)  # re-probes capabilities, no stale cache
```

The doctor never leaks secrets — `auth` reports presence, never the
file contents.

---

## 6. Install the collaboration skill

Teach the caller's agent **when and how** to delegate to `agy`.

```python
# User scope: installs into ~/.claude/skills/, ~/.agents/skills/,
# and ~/.agy/skills/
out = agy_install_skill(targets=["all"])

# Project scope: installs into <repo>/.claude/skills/, etc.
out = agy_install_skill(
    targets=["all"],
    scope="project",
    project_root="/Users/me/work/widgetsvc",
)

# Re-install after a skill body change. force=False is idempotent;
# pass force=True to rewrite even when the on-disk body matches.
out = agy_install_skill(targets=["claude"], force=True)

# out["installed"] is a list of:
#   {"target": "claude", "scope": "user",
#    "path": "~/.claude/skills/collaborating-with-antigravity/SKILL.md",
#    "overwrote": False}
# out["warnings"] surfaces per-file failures without aborting the rest.
```

The `antigravity` target lands under the wrapper-owned `~/.agy/skills/`
in user scope (project policy forbids writes under `~/.gemini/`).

---

## 7. Prune the local session store

Long-running projects accumulate per-job directories under
`~/.agy-mcp/sessions/`. Drop ones older than a threshold without
shelling out.

```python
# Drop sessions whose mtime is older than 30 days.
out = agy_purge(days=30)
# out["removed"]: list of dicts {job_id, age_days, ...} (paths redacted)
# out["removed_count"]: int, len(out["removed"])
# out["remaining"]: int, coarse count still on disk

# Refuses days <= 0 — guards against an off-by-one config wiping the
# entire store.
agy_purge(days=0)        # {"success": False, "error": "days must be > 0"}
agy_purge(days=-7)       # ditto
agy_purge(days=4000)     # also refuses (cap is 10 years)
```

The tool only touches directories whose name parses as a valid
`job_id` slug; arbitrary files under `~/.agy-mcp/sessions/` are
left alone.

---

## Going further

- `prompts/CLAUDE.md` and `prompts/AGENTS.md` are drop-in protocol
  snippets you can paste into a project's `CLAUDE.md` / `AGENTS.md` so
  every session in that repo knows when to call `agy`.
- `prompts/antigravity-system.md` is a system-prompt suggestion for
  the `agy` side, asking it to behave well as a collaborator (mention
  unverified assumptions, ask before writing, etc.).
- The full CLI flag set is documented in
  `skills/claude/collaborating-with-antigravity/references/usage.md`
  (also lands at `~/.claude/skills/.../references/usage.md` after
  `agy_install_skill`).
