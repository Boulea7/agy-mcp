# Installation

`agy-mcp` ships as a Python package with four console scripts. It targets
**Python 3.11+** and runs on macOS, Linux, and Windows.

## 1. Install the `agy` CLI (one-time, user-managed)

The bridge does NOT install or auto-update Google Antigravity for you. You
need a working `agy` on `PATH` before any non-dry-run call:

- Download from Google's distribution channel for your platform.
- Confirm with `agy --version` (the bridge probes this).
- Run `agy` once and complete its interactive login flow; the bridge
  refuses to run while `~/.gemini/oauth_creds.json` is missing
  (`agy_doctor` will tell you).

Optional fallback: `gemini` CLI v0.42+. If both binaries are on `PATH`, the
adapter prefers `agy`; pass `backend="gemini"` (or set
`AGY_MCP_BACKEND=gemini`) to override.

## 2. Install `agy-mcp`

The recommended path is `uv tool install` — fast, isolated, and easy to
upgrade.

```bash
# Install uv if you don't have it. The curl-pipe-sh form below is a
# trust delegation to astral.sh; if you'd rather verify the installer
# before running it, download to a file and check the SHA-256 first:
#
#   curl -fsSL -o /tmp/uv-install.sh https://astral.sh/uv/install.sh
#   shasum -a 256 /tmp/uv-install.sh   # or `sha256sum` on Linux
#   # cross-reference against the hash published on https://astral.sh/uv/install.sh
#   sh /tmp/uv-install.sh
#
# Or skip the installer entirely and use Homebrew / pipx if you
# already trust those:
#   brew install uv
#   pipx install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install agy-mcp directly from the git repo
uv tool install --from git+https://github.com/Boulea7/agy-mcp.git agy-mcp
```

`pip install` also works:

```bash
pip install --user "agy-mcp @ git+https://github.com/Boulea7/agy-mcp.git"
```

This puts four commands on `PATH`:

| Command | Purpose |
|---|---|
| `agymcp` | FastMCP stdio server (used by `claude mcp add` / Codex `mcp_servers.agy`) |
| `agy-bridge` | Standalone JSON-bridge CLI (used by Skills via `agy_bridge.py` forwarder) |
| `agy-doctor` | Environment + auth probe |
| `agy-install-skill` | Install the SKILL bundle into Claude / Codex / Antigravity skill dirs |

## 3. Register `agymcp` as an MCP server

### Claude Code

```bash
claude mcp add agy -s user --transport stdio -- uvx --from git+https://github.com/Boulea7/agy-mcp.git agymcp
```

Or, if you installed with `uv tool install`:

```bash
claude mcp add agy -s user --transport stdio -- agymcp
```

Verify it registered:

```bash
claude mcp list
```

### OpenAI Codex

Add to your Codex `config.toml` (typically `~/.codex/config.toml`):

```toml
[mcp_servers.agy]
command = "uvx"
args = ["--from", "git+https://github.com/Boulea7/agy-mcp.git", "agymcp"]
```

Or with a pinned tool install:

```toml
[mcp_servers.agy]
command = "agymcp"
```

## 4. Install the SKILL bundle

The MCP server makes 10 tools available, but the SKILL bundle teaches the
agent **when and how** to call them. Without it, callers have to discover
the tool surface on their own.

```bash
# User scope (recommended) — installs into ~/.claude/skills/,
# ~/.agents/skills/, and ~/.agy/skills/
agy-install-skill --target all

# Project scope — installs into <repo>/.claude/skills/,
# <repo>/.agents/skills/, and <repo>/.antigravity/skills/
agy-install-skill --target all --scope project --project-root /path/to/repo

# List what would land where (no writes)
agy-install-skill --list-targets
```

| Target | User-scope path | Project-scope path |
|---|---|---|
| `claude` | `~/.claude/skills/collaborating-with-antigravity/` | `<root>/.claude/skills/collaborating-with-antigravity/` |
| `codex` | `~/.agents/skills/collaborating-with-antigravity/` | `<root>/.agents/skills/collaborating-with-antigravity/` |
| `antigravity` | `~/.agy/skills/agy-collaboration/` | `<root>/.antigravity/skills/agy-collaboration/` |

The `antigravity` user-scope target lands under the wrapper-owned
`~/.agy/` directory rather than `~/.gemini/`; the latter is Antigravity's
own state directory and the project policy refuses to write there.

## 5. Verify

```bash
# Sanity: probe environment without making any agy API calls
agy-bridge --cd . --PROMPT "Hello" --mode ask --dry-run --debug

# Full environment report (Python, uv, agy/gemini binaries, OAuth, session store)
agy-doctor
```

You should see a JSON envelope with `success=true`, a `command_preview`
field showing the would-be argv (in dry-run mode), no secrets in any
field, and `auth.ok=true` once the interactive `agy` login flow has run.

## 6. Project snippets

If you want every Claude Code or Codex session in a given repo to know
about the bridge, copy the project-facing snippets:

- `prompts/CLAUDE.md` → append to your repo's `CLAUDE.md`
- `prompts/AGENTS.md` → append to your repo's `AGENTS.md`

These describe **when** to delegate to `agy` and which tool to use; the
SKILL.md (installed in step 4) describes **how**.

## Upgrade

```bash
uv tool upgrade agy-mcp
```

After upgrading the `agy` CLI in place, call the doctor with
`force_refresh=True` so the bridge re-probes capabilities instead of
returning a cached version:

```python
# From an MCP-aware caller (Claude Code, Codex):
agy_doctor(force_refresh=True)
```

```bash
# Or from the shell (no MCP client running):
agy-doctor
```

The shell variant always re-probes; the MCP tool caches between
calls because the probe shells out to `agy --help` / `agy --version`
and shouldn't repeat per invocation.

## Uninstall

```bash
uv tool uninstall agy-mcp
```

The installed SKILL bundles are NOT removed automatically. The packaged
files are:

- `~/.claude/skills/collaborating-with-antigravity/SKILL.md`
- `~/.claude/skills/collaborating-with-antigravity/scripts/agy_bridge.py`
- `~/.claude/skills/collaborating-with-antigravity/references/{usage,prompt-patterns,security}.md`
- `~/.agents/skills/collaborating-with-antigravity/` — same five files
- `~/.agy/skills/agy-collaboration/SKILL.md`
- `~/.agy/skills/agy-collaboration/references/collaboration.md`

If you have not added local overrides to those directories, remove
them with:

```bash
# Inspect first — these are the canonical files install_skills writes.
# Local overrides under the same paths would be lost.
ls -la ~/.claude/skills/collaborating-with-antigravity/ \
       ~/.agents/skills/collaborating-with-antigravity/ \
       ~/.agy/skills/agy-collaboration/ 2>/dev/null

# Once verified there's nothing of your own under those paths:
rm -rf ~/.claude/skills/collaborating-with-antigravity \
       ~/.agents/skills/collaborating-with-antigravity \
       ~/.agy/skills/agy-collaboration
```

`agy-mcp` never writes anywhere under `~/.gemini/`, so removing it
cannot affect Antigravity's own state directory.
