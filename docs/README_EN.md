# agy-mcp (English)

> **Skill-first, MCP-second** bridge from Claude Code / OpenAI Codex
> to **Google Antigravity CLI** (`agy`).
> Inherits the operational discipline of
> [`upstream/reference`](https://example.invalid/upstream) and
> [`upstream/reference`](https://example.invalid/upstream),
> extended with capability detection, async long-task supervision,
> dual-backend routing (`agy` + `gemini-cli` compatibility fallback),
> safe-by-default execution, Codex/Antigravity skills, cross-platform
> doctor, and a stable stream-json-compatible event schema.

[Chinese README](../README.md).

---

## What it is

A wrapper that turns Google's new Antigravity CLI (`agy`) into an
**agent backend Claude Code or OpenAI Codex can call directly**:

- **Skill-first**: SKILL bundles in `~/.claude/skills/` /
  `~/.agents/skills/` teach the agent **when** to delegate to `agy`
  and **how** to call it.
- **MCP-second**: the `agymcp` FastMCP server exposes 9 stable JSON
  tools, so the caller always gets a structured envelope.
- **Safe by default**: every error, log line, and response field is
  routed through `SafetyPolicy.redact`; write-enabled runs are
  isolated in a disposable git worktree; argv injection, path
  traversal, and parent-symlink swaps have targeted defences.

The bridge does NOT touch `agy`'s real API beyond capability probes
(`--help` / `--version`). It is a hardened gateway, not a re-host.

## 5-minute quickstart

```bash
# 1. Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install agy-mcp
uv tool install --from git+https://github.com/Boulea7/agy-mcp.git agy-mcp

# 3. Register the MCP server with Claude Code
claude mcp add agy -s user --transport stdio -- agymcp

# 4. Install the collaboration SKILL (Claude / Codex / Antigravity)
agy-install-skill --target all

# 5. Verify (no real agy API calls)
python -m agy_mcp.doctor
agy-bridge --cd . --PROMPT "Hello" --mode ask --dry-run --debug
```

Full installation and Codex setup in
[`docs/installation.md`](installation.md).

## 9 MCP tools

| Tool | Purpose |
|---|---|
| `agy` | Synchronous one-shot call (upstream-reference-compatible args + `mode` / `backend` / `output_protocol` / `worktree` / `allow_write` / `extra_env`) |
| `agy_continue` | Resume a prior `SESSION_ID` |
| `agy_start` | Background job; returns `job_id` immediately |
| `agy_status` | running / completed / failed / cancelled |
| `agy_read` | Read event stream (raw / claude / codex protocols) |
| `agy_cancel` | Cross-platform process-group cancel (POSIX `killpg` / Windows `CTRL_BREAK_EVENT`) |
| `agy_sessions` | List recent sessions with mtime / status / cwd summary |
| `agy_doctor` | Environment + auth + capability probe (no secrets) |
| `agy_install_skill` | Install SKILL bundle into Claude / Codex / Antigravity skill dirs |

## When to use

| Task type | Path |
|---|---|
| Q&A you can answer from the open context | Don't delegate, just answer |
| Second opinion on a bug hypothesis | `agy(..., mode="review")` |
| Generate a diff for review | `agy(..., mode="prototype")` (no `allow_write`) |
| Apply a reviewed diff | `agy(..., mode="execute", allow_write=True)` (auto worktree) |
| Multi-hour refactor | `agy_start(..., mode="long")` then poll |

## When NOT to use

- Trivial questions you can answer directly: the round-trip isn't
  worth it.
- Anything that needs the actual Anthropic / OpenAI conversation
  state: `agy` is a separate model with its own context.

## Safety floor

- Every error / log / response is run through
  `SafetyPolicy.redact`: `/Users/<u>/` → `~/`; PEM / JWT / AKID /
  Bearer / Authz are all scrubbed.
- `allow_write=True` is the hard gate for any mutation; destructive
  prompts are refused even with the flag set.
- `execute` mode refuses prompts that read or mention `~/.ssh`,
  `~/.aws/credentials`, browser cookie stores, OS keychain.
- `mode=execute --allow-write` defaults to `worktree=True`
  (overridable via `~/.config/agy-mcp/config.toml` or
  `AGY_MCP_WORKTREE_DEFAULT=0`).
- We never write under `~/.gemini/` (Antigravity's own state dir);
  the user-scope antigravity SKILL lands under the wrapper-owned
  `~/.agy/skills/`.

Full threat model and the explicit "what is NOT defended" list:
[`security.md`](security.md).

## Project-side snippets

Drop into the repo's `CLAUDE.md` / `AGENTS.md` so every session in
that repo knows when to call `agy`:

- [`prompts/CLAUDE.md`](../prompts/CLAUDE.md) — Claude Code collaboration protocol
- [`prompts/AGENTS.md`](../prompts/AGENTS.md) — OpenAI Codex collaboration protocol
- [`prompts/antigravity-system.md`](../prompts/antigravity-system.md) — system prompt
  suggestion for the `agy` side

## Documentation

| File | Contents |
|---|---|
| [`installation.md`](installation.md) | Install + Claude Code / Codex registration + SKILL install + verification |
| [`architecture.md`](architecture.md) | Module map (caller / MCP server / bridge / supervisor / adapter / safety) |
| [`output-strategy.md`](output-strategy.md) | Hybrid backend: stdout + klog + transcript.jsonl + protocol translator |
| [`security.md`](security.md) | Threat model, defence catalogue, explicit non-defences |
| [`cli-capabilities.md`](cli-capabilities.md) | Live `agy --help` + capability matrix (refresh when CLI is upgraded) |
| [`examples.md`](examples.md) | 6 scenarios: review, prototype, long, continue, doctor, install |
| [`comparison-with-upstream-reference.md`](comparison-with-upstream-reference.md) | What we inherited / extended / changed |
| [`reference-review.md`](reference-review.md) | Design notes from the two `upstream` reference repos |
| [`review-followups.md`](review-followups.md) | Per-phase review P3 follow-ups |

## Development

```bash
uv sync
uv run pytest        # 392 tests
uv run agymcp        # FastMCP stdio server (manual testing)
uv run agy-bridge --cd . --PROMPT "Hello" --mode ask --dry-run --debug
```

The two `upstream` reference repos live under `.refs/` (gitignored).
Clone them locally for comparison:

```bash
git clone https://example.invalid/upstream .refs/upstream-reference
git clone https://example.invalid/upstream \
    .refs/upstream-reference
```

## License

[MIT](../LICENSE).
