# agy-mcp (English)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](../pyproject.toml)
[![CI](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-525%20passed-brightgreen.svg)](#)
[![中文](https://img.shields.io/badge/%E4%B8%AD%E6%96%87-README-red.svg)](../README.md)

> Wraps Google **Antigravity CLI** (`agy`) as 10 typed MCP tools any MCP
> client (Claude Code / OpenAI Codex / Cursor / Cline / Continue …) can
> call directly. Ships with optional Skill bundles that teach
> skill-aware platforms *when* to delegate and *which mode* to use.

---

## Quickstart

```bash
# 1. Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install agy-mcp
uv tool install --from git+https://github.com/Boulea7/agy-mcp.git agy-mcp

# 3. Register the MCP server (Claude Code shown; others below)
claude mcp add agy -s user --transport stdio -- agymcp

# 4. (Optional) install SKILLs so Claude / Codex / Antigravity learn when to call
agy-install-skill --target all

# 5. Verify (no real agy API calls)
agy-doctor
```

<details>
<summary><strong>Let your local agent install it</strong> (recommended: paste the prompt below into Claude Code / Codex — it will read, execute, and verify)</summary>

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
4-line summary: where it landed, whether all 10 MCP tools are
exposed, where the SKILL files live, and what remains optional.
````

</details>

<details>
<summary><strong>Other MCP clients</strong></summary>

- **OpenAI Codex CLI** — append to `~/.codex/config.toml`:
  ```toml
  [mcp_servers.agy]
  command = "agymcp"
  args = []
  ```
  Restart the Codex session.
- **Cursor / Cline / Continue / other MCP clients** — add a server
  with name=`agy`, command=`agymcp`, transport=stdio in the client's
  MCP config. Each client's exact syntax differs; see its docs.

</details>

Full install + troubleshooting → [`installation.md`](installation.md).

---

## What it is

A wrapper that turns Google's new Antigravity CLI (`agy`) into a
collaboration backend any MCP client can call. Two equivalent paths:

- **MCP server**: `agymcp` exposes 10 typed JSON tools over FastMCP
  stdio with stable pydantic envelopes. **Any MCP client.**
- **Skill bundles**: install into `~/.claude/skills/`,
  `~/.agents/skills/`, `~/.agy/skills/`. Teach the agent *when* to
  delegate, *which mode* to use, and which safety rules to follow.
  **Only Claude Code / OpenAI Codex / Antigravity.**
- **Shared backend**: both paths share the same `bridge.py` → adapter
  → safety policy → worktree pipeline, so behaviour is identical.

> Outside `agy_doctor` and `--dry-run` paths, `agy` / `agy_start`
> spawn real `agy --print` and may consume Antigravity request quota.
> The project wraps, routes, isolates, and audits the CLI; it does
> not reimplement the `agy` API.

## 10 MCP tools

| Tool | Purpose |
|---|---|
| `agy` | Synchronous one-shot call (PROMPT / cd / sandbox / SESSION_ID + `mode` / `backend` / `output_protocol` / `worktree` / `allow_write` / `extra_env`) |
| `agy_continue` | Resume a prior `SESSION_ID` |
| `agy_start` | Background long job; returns `job_id` immediately |
| `agy_status` | Poll job state: running / completed / failed / cancelled |
| `agy_read` | Read job event stream (raw / claude / codex protocols) |
| `agy_cancel` | Cross-platform process-group cancel |
| `agy_sessions` | List recent sessions |
| `agy_doctor` | Env + auth + capability probe (no secrets) |
| `agy_install_skill` | Install SKILL bundles into Claude / Codex / Antigravity dirs |
| `agy_purge` | Prune local session-store directories (refuses `days <= 0`) |

## When to use / When NOT to use

| Situation | Path |
|---|---|
| Q&A answerable from open context | Don't delegate, just answer |
| Second opinion on a bug hypothesis | `agy(..., mode="review")` |
| Diff for review | `agy(..., mode="prototype")` (no `allow_write`) |
| Apply a reviewed diff | `agy(..., mode="execute", allow_write=True)` (auto worktree) |
| Multi-hour refactor | `agy_start(..., mode="long")` then poll |
| Anything needing the Anthropic / OpenAI conversation state | Don't delegate — `agy` is a separate model with its own context |

## Safety floor

- Every error / log / response field passes through
  `SafetyPolicy.redact`: `/Users/<u>/` → `~/`; PEM / JWT / AKID /
  Bearer / Authz are all scrubbed.
- `mode=execute` mutations require explicit `allow_write=True`;
  destructive prompts are refused even with the flag.
- `execute` mode refuses prompts that read or mention `~/.ssh`,
  `~/.aws/credentials`, browser cookie stores, OS keychain.
- `mode=execute + allow_write` defaults to `worktree=True`
  (overridable via `~/.config/agy-mcp/config.toml` or
  `AGY_MCP_WORKTREE_DEFAULT=0`).
- Never writes under `~/.gemini/` (Antigravity's own state dir);
  the user-scope antigravity SKILL lands under `~/.agy/skills/`.

Full threat model and the explicit "what is NOT defended" list →
[`security.md`](security.md).

## Project-side snippets

Drop into the repo's `CLAUDE.md` / `AGENTS.md` so every session in
that repo knows when to call `agy`:

- [`prompts/CLAUDE.md`](../prompts/CLAUDE.md) — Claude Code collaboration protocol
- [`prompts/AGENTS.md`](../prompts/AGENTS.md) — OpenAI Codex collaboration protocol
- [`prompts/antigravity-system.md`](../prompts/antigravity-system.md) — system prompt suggestion for the `agy` side

## Documentation

| File | Contents |
|---|---|
| [`installation.md`](installation.md) | Install + Claude Code / Codex registration + SKILL + verification |
| [`architecture.md`](architecture.md) | Module map (caller / MCP server / bridge / supervisor / adapter / safety) |
| [`output-strategy.md`](output-strategy.md) | Hybrid backend: stdout + klog + transcript.jsonl + protocol translator |
| [`security.md`](security.md) | Threat model, defence catalogue, explicit non-defences |
| [`cli-capabilities.md`](cli-capabilities.md) | Live `agy --help` + capability matrix |
| [`examples.md`](examples.md) | 6 end-to-end scenarios |
| [`../CHANGELOG.md`](../CHANGELOG.md) | Version history (Keep a Changelog) |

## Development

```bash
uv sync
uv run pytest        # full suite — 525 tests
uv run agymcp        # FastMCP stdio server (manual testing)
uv run agy-bridge --cd . --PROMPT "Hello" --mode ask --dry-run --debug
uv run agy-doctor    # environment and auth probe
```

## License

[MIT](../LICENSE).
