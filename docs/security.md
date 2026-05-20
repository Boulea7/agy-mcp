# Security model

This document describes the threat model, what `agy-mcp` defends, and
what it explicitly does **not** defend. Read this before changing
anything in `safety.py`, `worktree.py`, `install.py`, or
`utils.safe_write_text`.

## Threat model

The bridge sits between two trust boundaries:

1. **Caller → bridge** (Claude Code / Codex → MCP stdio): the caller is
   trusted with the user's repo and shell, but the prompt content may
   be adversarial (user input, scraped issue body, etc.). The bridge
   must not let a hostile prompt escape its sandbox.
2. **Bridge → agy** (subprocess launch): the bridge controls argv,
   environment, and cwd. The `agy` binary itself is trusted (it was
   installed by the user) but its responses are NOT — they're streamed
   back into the caller's transcript.

The bridge is **not** a sandbox for `agy` — that's `agy --sandbox`'s
job. It's a hardened gateway that:

- Refuses requests likely to leak secrets or perform destructive
  operations.
- Scrubs response and error content before returning to the caller.
- Isolates write-enabled `execute` runs in a disposable git worktree.

## Layered defences

### 1. Request validation (`models.py::BridgeRequest`)

Pydantic v2 with `extra="forbid"`. Notable validators:

- `prompt`: 1 ≤ length ≤ 256_000 characters (well under any platform's
  argv ceiling when fused as `--print=<value>`). Also passed to
  `SafetyPolicy`'s pattern-based deny-list.
- `extra_env` (passed through to `agy` subprocess): keys must match
  `^[A-Z_][A-Z0-9_]*$`, values cannot contain `\n` / `\r` / NUL, max
  64 entries, max 4096 chars per value. Rejects the POSIX-special `_`
  key and runtime-control names such as `NODE_OPTIONS`, `PYTHON*`,
  `LD_*`, `DYLD_*`, `GIT_CONFIG*`, `PATH`, `HOME`,
  `BASH_ENV`, `ENV`, `AGY_CLI_DISABLE_AUTO_UPDATE`, and
  `ANTIGRAVITY_CONVERSATION_ID`. (Phase 5 R2 sec P0-1.)
- `mode`, `backend`, `output_protocol`: closed enums.
- `timeout`: 1 ≤ value ≤ 86400 (24h ceiling; longer runs should use
  `mode="long"` + `agy_start`).
- `max_output_chars`: 1 ≤ value ≤ 8 MiB (caps the in-process buffered
  transcript; detached `agy_start` jobs also persist the full event
  stream in `events.jsonl`).
- `SESSION_ID` / `session_id`: max 96 chars before it can seed a
  conversation resume or worktree name.

### 2. Deny-list (`safety.py::SafetyPolicy`)

Reads from / mentions of sensitive paths are refused outright in
`execute` mode and warned in other modes:

- `~/.ssh/`, `~/.aws/credentials`, `~/.gnupg/`, `~/.config/gh/`
- Browser cookie stores (`~/Library/Application Support/Google/Chrome`,
  `~/.mozilla/firefox/.../cookies.sqlite`, etc.)
- OS keychain (`security find-generic-password`, `secret-tool`)
- Destructive command shapes: `rm -rf /`, `dd if=/dev/zero of=/dev/...`,
  `mkfs`, `:(){:|:&};:`, etc.

The deny-list is applied to both the prompt body AND the synthesised
argv (defence-in-depth: a prompt that ends up in `--extra-args` still
gets scanned).

### 3. argv injection (`bridge.py` + `adapters/agy.py`)

Every flag that takes a value is fused as `--flag=value` instead of
two argv items (`--flag`, `value`). This stops a malicious value from
being parsed as a new flag. The fused form is what we pass to
`subprocess.Popen(shell=False)`.

### 4. Subprocess hygiene

- `shell=False` always; argv only.
- `start_new_session=True` (POSIX) / `CREATE_NEW_PROCESS_GROUP`
  (Windows) so cancellation can `killpg(SIGTERM)` / send
  `CTRL_BREAK_EVENT` without losing the whole tree.
- `stdin=DEVNULL` — `agy` is never given interactive input.
- Environment is **filtered**, not inherited: start from
  `os.environ.copy()`, drop any key matching `SECRET_ENV_NAME_PATTERN`
  (regex covering `*TOKEN`, `*API_KEY`, `*SECRET`, `*PASSWORD`,
  `*CRED*`, etc.) PLUS the explicit `DEFAULT_SCRUB_ENV_NAMES` list
  (`AWS_*`, `GCP_*`, `AZURE_*`, `OPENAI_*`, `ANTHROPIC_*`,
  `GH_TOKEN`, `GITHUB_TOKEN`, `NPM_TOKEN`, `PYPI_TOKEN`, etc. — 32
  entries). The regex and the list run in tandem so an env name like
  `MY_CUSTOM_API_KEY` (regex match) and `AWS_PROFILE` (explicit list
  match) both get dropped.

### 5. File-write primitive (`utils.py::safe_write_text`)

Used by `session_store`, `install`, and `worktree`. When
`verify_under` is provided on POSIX platforms with `openat` support, it
defends against parent-symlink swaps by:

- Opening the resolved `verify_under` root once with
  `O_DIRECTORY|O_NOFOLLOW`.
- Creating or opening each intermediate directory via `dir_fd` with
  `O_NOFOLLOW`.
- Creating a random tempfile with `O_CREAT|O_EXCL|O_NOFOLLOW` against
  the pinned parent directory fd.
- Renaming the tempfile to the final leaf with `src_dir_fd` and
  `dst_dir_fd`, so the final publish does not re-resolve the parent path.

On Windows or filesystems that do not expose the required `openat`
family, the fallback path still does a pre-write and post-rename
symlink walk with `relative_to(verify_under)` checks. That fallback is
detect-after-the-fact for a successful parent swap; the POSIX openat
path is the airtight path.

### 6. Worktree isolation (`worktree.py`)

When `mode=execute` and `allow_write=True`, the bridge:

- Creates `<repo>/.agy-mcp/worktrees/<session_id>/` with
  `git worktree add` on a fresh branch.
- Runs the child process with `cwd` set to the worktree so the agent's
  edits land there.
- Leaves the worktree in place after the run so the caller can inspect,
  merge, or discard the branch. Remove it manually with
  `git worktree remove <path>` when finished.

Configurable via `~/.config/agy-mcp/config.toml`:

```toml
[execute]
worktree_default = true     # opt-out via false
```

Env var override: `AGY_MCP_WORKTREE_DEFAULT=0/1`.

### 7. Output redaction (`safety.py::SafetyPolicy.redact`)

Every string that leaves the process (`error`, `warnings`,
`agent_messages`, `installed[*].path`, `command_preview`, log lines):

- PEM blocks → `***`
- JWT tokens → `***`
- AWS access key IDs (`AKIA...`) → `***`
- `Bearer <token>` / `Authorization: <scheme> <token>` → `Bearer ***` / `Authorization: <scheme> ***`. The same redaction is applied to a wider header allow-list driven by `_AUTHZ_HEADER` (`utils.py:62-66`): `Authorization`, `X-Api-Key`, `X-Auth-Token`, `X-Auth-Key`, `Api-Key`, `Apikey`, `Proxy-Authorization`, `X-Goog-Api-Key`, `X-OpenAI-Key`, `X-Anthropic-Key`.
- Slack tokens (`xoxb-…`, `xoxp-…`) → `***`
- GitHub fine-grained PATs (`github_pat_…`) → `***`
- Generic high-entropy key=value secrets → `***`
- `/Users/<u>/` → `~/`, `/home/<u>/` → `~/`, `C:\Users\<u>\` → `~\`

The placeholder is the opaque token `***` (defined as
`utils.REDACTION_PLACEHOLDER`) rather than a typed marker like
`<REDACTED PEM>`. The opacity is deliberate: a tagged placeholder
would tell an observer the original value type, giving an attacker
an oracle on what kind of secret leaked. Operators auditing logs
should treat any `***` as "credential-shaped material was redacted
here"; the exact type lives only in the process that did the
redaction, not in the persisted output.

Compiled patterns are cached behind a `threading.RLock` so two
concurrent MCP tool calls cannot race on first redaction. The lock
is re-entrant so a future custom user pattern that itself raises and
gets re-redacted in an `except` block via `_extra_patterns` will not
deadlock.

### 8. MCP tool surface guards (`server.py`)

- `agy_install_skill`: `targets` capped at 16 entries, each rejected
  unless it is `str` and `in {"claude", "codex", "antigravity", "all"}`.
  `scope` allow-listed. `project_root` validated (leaf is not a
  symlink) before `install_skills` runs. Deliberate
  defence-in-depth with `_expand_targets` doing the same allow-list
  check (Phase 7 R1 arch P2-2). The leaf check is only the surface
  layer; the **ancestor symlink-swap window** is closed at write
  time by the `safe_write_text` parent walk described in § 5.
- `agy_status` / `agy_read` / `agy_cancel`: `job_id` must match
  `^job_[A-Za-z0-9_-]{1,80}$`. Oversized values are rejected with a
  structured error.
- All sync tools route through `_structured_failure` on exception —
  never a bare traceback to the caller.

## What is NOT defended

Documenting what the bridge does NOT defend prevents callers from
assuming protection that isn't there.

- **Compromised `agy` binary.** If `agy` itself is hostile, the bridge
  cannot detect it. We probe `agy --help` / `agy --version` once for
  capability detection and trust the output.
- **Caller-side prompt leaks.** The caller can paste the bridge's
  response wherever it likes. We redact secrets before returning, but
  if the response is logged into an external system the caller is on
  the hook.
- **`AGY_BRIDGE_CMD` env var.** This is an advanced override consumed
  by `skills/.../scripts/agy_bridge.py` that lets the forwarder shell
  out to an arbitrary command. Treat it as a trust boundary: anything
  with `AGY_BRIDGE_CMD` set can run that command with the user's
  privileges.
- **Editable installs.** `pip install -e .` / `uv pip install -e .`
  makes `_skill_bodies/` read from the working tree at install time,
  so a hostile working tree feeds hostile install content. By design
  — the developer is trusted on their own machine.
- **TOCTOU residue on the non-openat fallback path.** On Windows or
  filesystems that do not support the required `openat` APIs,
  `safe_write_text`'s post-walk surfaces the breach to the caller (via
  a raised `OSError`) but does NOT undo a published leaf if an attacker
  wins the race. POSIX platforms with openat support use the anchored
  dir-fd path described in § 5.
- **Versioned `uvx` fallback availability.** The skill forwarder uses a
  fixed package version (`agy-mcp==0.1.0`) rather than a mutable branch
  ref. If that version is not published in the user's configured Python
  index, the fallback fails closed and the user must install the bridge
  locally or set `AGY_BRIDGE_CMD`.
- **System-level symlinks on macOS / Linux.** `/tmp/...` →
  `/private/tmp/...` and `/var/...` → `/private/var/...` are honest
  symlinks but they exist on every macOS install. `_validate_project_root`
  intentionally does not refuse paths whose ancestors include such
  symlinks; the write-time `safe_write_text` walk under the resolved
  root provides the real defence.

## Audit hooks

The session store records every job, including:

- The redacted `command_preview` (argv after `_structured_failure`'s
  redact pass).
- The full `events.jsonl` stream (raw canonical events, never
  truncated).
- `stdout.log`, `stderr.log`, `agy.log` (the klog file).
- `meta.json` with `created_at`, `updated_at`, `exit_code`,
  `cancel_reason`.

An operator can review past runs with `agy_sessions()` +
`agy_read(job_id, translate="raw")` without needing the original
caller's transcript.
