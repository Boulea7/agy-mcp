# Security reference

`agy-bridge` and the supervisor are designed for **local trusted
operators**. The threat model is:

- The MCP caller (Claude Code, Codex, etc.) is trusted.
- The `agy` binary and its OAuth credentials are trusted.
- The shell and PATH are operator-controlled; we defend against
  accidental leaks, not active hostility on the local machine.

## Secret handling

`SafetyPolicy` redacts every string that crosses a boundary:

- **Patterns** scrubbed in error / log / response text:
  - PEM private keys (`-----BEGIN ... PRIVATE KEY-----` blocks)
  - JWT-style `eyJ...` tokens
  - `Bearer <token>` / `Authorization: <…>` headers
  - AWS-style `AKIA…` access key ids
  - Anything matching `*_KEY_*` / `*_TOKEN_*` in env / argv
- **Path anonymisation**: `/Users/<u>/` → `~/` and equivalents on
  Windows / Linux. This keeps the MCP transcript safe for sharing.

The scrubber runs on `BridgeResponse.error`, every adapter warning,
the doctor report, and every install / status / cancel envelope.

## Env scrub list

`SafetyPolicy` drops these env names before spawning a child (full
canonical list in `src/agy_mcp/safety.py::DEFAULT_SCRUB_ENV_NAMES`):

```
ANTHROPIC_API_KEY  OPENAI_API_KEY  GEMINI_API_KEY
GOOGLE_API_KEY  GOOGLE_APPLICATION_CREDENTIALS
GITHUB_TOKEN  GH_TOKEN  GITHUB_PAT  GITLAB_TOKEN
HF_TOKEN  HUGGINGFACE_TOKEN
AWS_ACCESS_KEY_ID  AWS_SECRET_ACCESS_KEY  AWS_SESSION_TOKEN
AZURE_OPENAI_API_KEY  AZURE_CLIENT_SECRET  VERTEX_AI_API_KEY
DATABRICKS_TOKEN  STRIPE_API_KEY
SLACK_BOT_TOKEN  SLACK_USER_TOKEN
NPM_TOKEN  PYPI_TOKEN
DATABASE_URL  DATABASE_URI  REDIS_URL
MONGODB_URI  POSTGRES_URL
KUBECONFIG  SENTRY_DSN  VAULT_TOKEN  KAGGLE_KEY
```

Extend the list in `~/.config/agy-mcp/config.toml`:

```toml
[safety]
scrub_extra_env = ["MY_INTERNAL_TOKEN"]
```

## Argv / prompt deny-list

The policy denies obvious destructive patterns regardless of `mode`:

- `rm -rf /` and family
- `dd if=… of=/dev/…`
- `:(){ :|:& };:` fork bombs
- `chmod 777` against system paths
- `mkfs` against block devices

Sensitive-path mentions (`~/.ssh`, `~/.aws/credentials`, browser
cookies, OS keychain entries) are **warned** in `ask` / `plan` /
`review` modes and **blocked** in `execute` mode. The block is
substring-based, so a prompt that reads, writes, or even mentions
these paths in `execute` is refused.

## Worktree behaviour

When the policy decides a write is allowed (`execute` + `--allow-write`
in a git workspace), the bridge creates a git worktree under
`<repo>/.agy-mcp/worktrees/<session-id>/` and runs the child there.
The worktree remains after exit so you can inspect and merge the branch;
remove it with `git worktree remove <path>` when finished.

Disable via:
- `--worktree false` per call
- `AGY_MCP_WORKTREE_DEFAULT=0` env var
- `execute.worktree_default = false` in `~/.config/agy-mcp/config.toml`

## extra_env validator

The MCP layer enforces the same rules as the CLI:

- Names must match `^[A-Z_][A-Z0-9_]*$`.
- Values must not contain `\0` / `\r` / `\n`.
- At most 64 entries; each value at most 4096 chars.

This stops a hostile MCP caller from smuggling a fake second variable
via `FOO=bar\nLD_PRELOAD=…`.

## Audit log layout

Every supervisor-managed job writes to:

```
~/.agy-mcp/sessions/<job_id>/
├── meta.json          # JobRecord (timestamps, status, exit code, pid)
├── events.jsonl       # CanonicalEvent stream (NDJSON)
├── stdout.log         # raw subprocess stdout
├── stderr.log         # raw subprocess stderr
├── agy.log            # the agy CLI's --log-file output (klog)
└── artifacts/         # any files the adapter chose to preserve
```

Permissions are `0o700` on the root and `0o600` on files (POSIX).
Retention is governed by `session_store.retention_days` (default 30).

## What is NOT defended

- A local attacker with write access to your `~/.gemini/oauth_creds.json`
  can impersonate you to Google. We detect symlinks but not content
  tampering — the doctor only checks file shape.
- A local attacker who can edit `~/.config/agy-mcp/config.toml` can
  disable safety. Treat the config file as sensitive.
- A local attacker who can set ``AGY_BRIDGE_CMD`` in the operator's
  environment runs as the operator — this env var is a **trust
  boundary**, since the skill forwarder splits and executes its
  value verbatim. Vet anything that exports it (`.envrc`, `direnv`,
  shell init files) before trusting the bridge invocation.
- We do not attempt to defeat a hostile `agy` binary; if PATH is
  poisoned, we run the wrong binary. The doctor reports the resolved
  path; verify it before trusting the session.
