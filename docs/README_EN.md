# agy-mcp (English)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](../pyproject.toml)
[![CI](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-513%20passed-brightgreen.svg)](#)
[![中文](https://img.shields.io/badge/%E4%B8%AD%E6%96%87-README-red.svg)](../README.md)

> **Skill-first, MCP-second** bridge from Claude Code / OpenAI Codex
> to **Google Antigravity CLI** (`agy`).
> Ships with capability detection, async long-task supervision,
> dual-backend routing (`agy` + `gemini-cli` compatibility fallback),
> safe-by-default execution, Codex / Antigravity skills, cross-platform
> doctor, and a stable stream-json-compatible event schema.

---

## One-shot install (recommended: let your local agent do it)

Copy the block below and paste it into your local **Claude Code** or
**OpenAI Codex CLI**. It will read, execute, and verify the install
on its own — no manual command typing required.

````text
Please install the open-source MCP `agy-mcp` for me. Concretely:

1. Install this repo's console scripts (agymcp / agy-bridge /
   agy-doctor / agy-install-skill) with `uv tool`. If `uv` is missing,
   first run `curl -LsSf https://astral.sh/uv/install.sh | sh`.
2. Register the agy MCP server with the agent platform you are
   currently running on:
   - If you are Claude Code, run:
     `claude mcp add agy -s user --transport stdio -- agymcp`
     then confirm with `claude mcp list` that `agy: ✓ Connected`.
   - If you are OpenAI Codex CLI, append this to
     `~/.codex/config.toml`:
     ```toml
     [mcp_servers.agy]
     command = "agymcp"
     args = []
     ```
     and restart the Codex session so it takes effect.
3. Install the collaboration SKILL (so this platform knows when to
   delegate to agy): call the just-exposed `agy_install_skill` MCP
   tool with `targets=["all"], scope="user"`. It writes to
   `~/.claude/skills/`, `~/.agents/skills/`, and `~/.agy/skills/`.
   Tell me to restart the window so the SKILL is picked up.
4. Verify by calling the `agy_doctor` MCP tool and pasting the full
   JSON back to me. Expect `healthy=true` with all 6 checks
   `ok=true`. If the `auth` check shows "not logged in", tell me to
   run `agy --version` once manually to trigger the OAuth flow, then
   call `agy_doctor` again.
5. Install source: `git+https://github.com/Boulea7/agy-mcp.git`
   (public install) or local `--from <absolute project path>` if I
   point you at a local clone.

Constraints:
- Don't push anything, don't edit my .gitconfig.
- Until I confirm, don't make real `agy` model calls (doctor /
  dry-run only).
- Briefly describe each write step before doing it and wait for my
  nod; if your current permission mode allows acceptEdits, just
  proceed.

Report a one-liner per step. After everything is done, give me a
4-line summary: where it landed, whether all 9 MCP tools are
exposed, where the SKILL files live, and what remains optional.
````

> Already comfortable with manual installs? The
> [5-minute quickstart](#5-minute-quickstart) below still works.

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

Outside `agy-doctor` and `--dry-run` verification paths, normal `agy`
/ `agy_start` calls spawn `agy --print` and may consume real
Antigravity requests. The project wraps, routes, isolates, and audits
the CLI; it does not reimplement the `agy` API.

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
agy-doctor
agy-bridge --cd . --PROMPT "Hello" --mode ask --dry-run --debug
```

Full installation and Codex setup in
[`docs/installation.md`](installation.md).

## 9 MCP tools

| Tool | Purpose |
|---|---|
| `agy` | Synchronous one-shot call (standard PROMPT / cd / sandbox / SESSION_ID arg set + `mode` / `backend` / `output_protocol` / `worktree` / `allow_write` / `extra_env`) |
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
- `mode=execute` mutations require explicit `allow_write=True`;
  destructive prompts are refused even with the flag set.
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
| [`../CHANGELOG.md`](../CHANGELOG.md) | Version history (Keep a Changelog format) |

## Development

```bash
uv sync
uv run pytest        # full suite — 513 tests
uv run agymcp        # FastMCP stdio server (manual testing)
uv run agy-bridge --cd . --PROMPT "Hello" --mode ask --dry-run --debug
uv run agy-doctor    # environment and auth probe
```

## License

[MIT](../LICENSE).
