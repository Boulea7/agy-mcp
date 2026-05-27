# agy-mcp (日本語)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](../pyproject.toml)
[![CI](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-560%20passed-brightgreen.svg)](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml)

言語：[`简体中文`](../README.md) · [`繁體中文`](README_ZH-TW.md) · [`English`](README_EN.md)

> Google **Antigravity CLI**（`agy`）を 11 個の typed MCP tool として
> ラップし、任意の MCP client（Claude Code / OpenAI Codex / Cursor /
> Cline / Continue …）から直接呼び出せるようにします。任意で Skill
> bundle も提供し、skill 対応プラットフォームに*いつ委譲するか*、
> *どの mode を使うか*を教えます。

---

## クイックスタート

```bash
# 1. uv をインストール（既にある場合は不要）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. agy-mcp をインストール（PyPI から）
uv tool install agy-mcp

# 3. MCP server を登録（Claude Code の例。他の client は下記）
claude mcp add agy -s user --transport stdio -- agymcp

# 4. （任意）Claude / Codex / Antigravity が呼び出しタイミングを学べるよう SKILL をインストール
agy-install-skill --target all

# 5. 検証（実際の agy API は呼び出さない）
agy-doctor
```

<details>
<summary><strong>ローカル agent にインストールさせる</strong>（推奨：下のプロンプトを Claude Code / OpenAI Codex CLI に貼り付けると、読み取り、実行、検証まで行います）</summary>

````text
agy-mcp という open-source MCP をインストールしてください。具体的な目標：

1. uv tool でこの repo の console scripts（agymcp / agy-bridge /
   agy-doctor / agy-install-skill）をインストールする。uv がなければ、
   先に `curl -LsSf https://astral.sh/uv/install.sh | sh` を実行する。
2. 現在動いている agent platform に agy MCP server を登録する：
   - Claude Code の場合：
     `claude mcp add agy -s user --transport stdio -- agymcp`
     を実行し、`claude mcp list` で `agy: ✓ Connected` を確認する。
   - OpenAI Codex CLI の場合、`~/.codex/config.toml` に以下を追加する：
     ```toml
     [mcp_servers.agy]
     command = "agymcp"
     args = []
     ```
     その後 Codex session を再起動して反映する。
3. collaboration SKILL をインストールする（この platform がいつ agy に
   委譲すべきか分かるようにする）：登録された `agy_install_skill`
   MCP tool を `targets=["all"], scope="user"` で呼び出す。
   `~/.claude/skills/`、`~/.agents/skills/`、`~/.agy/skills/` に書き込まれる。
   完了後、SKILL を読み込むために window の再起動が必要だと伝える。
4. 検証：`agy_doctor` MCP tool を呼び出し、返ってきた JSON を全文貼る。
   期待値は `healthy=true`、主要 check が `ok=true`。`auth` が未ログインを
   示す場合は、手動で `agy --version` を一度実行して OAuth flow を起動し、
   その後もう一度 `agy_doctor` を呼び出すよう伝える。
5. インストール元はデフォルトで PyPI：`uv tool install agy-mcp`。
   未リリース branch や local clone を指定された場合のみ、
   `git+https://github.com/Boulea7/agy-mcp.git` または local
   `--from <project absolute path>` に切り替える。

制約：
- push しない。私の .gitconfig を変更しない。
- 私が確認するまで、実際の `agy` model call は行わない（doctor /
  dry-run のみ）。
- 書き込み操作の前には、何をするか短く説明し、私の承認を待つ。
  現在の permission mode が acceptEdits を許している場合は、そのまま進める。

各 step が終わるたびに 1 行で報告する。すべて終わったら 4 行でまとめる：
どこにインストールされたか、11 個の MCP tool が公開されたか、SKILL の
配置先、残っている任意項目。
````

</details>

<details>
<summary><strong>他の MCP client の登録方法</strong></summary>

- **OpenAI Codex CLI**：`~/.codex/config.toml` に以下を追加：
  ```toml
  [mcp_servers.agy]
  command = "agymcp"
  args = []
  ```
  Codex session を再起動します。
- **Cursor / Cline / Continue / その他の MCP client**：各 client の
  MCP server 設定に name=`agy`、command=`agymcp`、transport=stdio を
  追加します。正確な構文は client ごとに異なるため、それぞれの docs を
  参照してください。

</details>

完全なインストールとトラブルシューティング → [`installation.md`](installation.md)。

---

## これは何か

Google の新しい Antigravity CLI（`agy`）を、任意の MCP client から
呼び出せる collaboration agent backend にする wrapper です。2 つの
同等な経路を提供します：

- **MCP server**：`agymcp` が FastMCP stdio 経由で 11 個の typed JSON
  tool を公開します。pydantic envelope は安定して解析できます。
  **任意の MCP client で利用可能**です。
- **Skill bundles**：`~/.claude/skills/`、`~/.agents/skills/`、
  `~/.agy/skills/` にインストールし、agent に*いつ* agy へ委譲するか、
  *どの mode*を使うか、どの安全ルールに従うかを教えます。
  **Claude Code / OpenAI Codex / Antigravity のみ有効**です。
- **共有 backend**：どちらの経路も同じ `bridge.py` → adapter →
  safety policy → worktree pipeline を通るため、挙動は一致します。

> `agy_doctor` と `--dry-run` 以外では、`agy` / `agy_start` は実際の
> `agy --print` を起動し、Antigravity request quota を消費する可能性が
> あります。この project は CLI の wrap、routing、isolation、audit を
> 行うだけで、`agy` API を再実装しません。

## 11 個の MCP tool

| Tool | Purpose |
|---|---|
| `agy` | 同期 one-shot call（PROMPT / cd / sandbox / SESSION_ID + `mode` / `backend` / `output_protocol` / `worktree` / `allow_write` / `extra_env`） |
| `agy_continue` | 既存の `SESSION_ID` を再開 |
| `agy_start` | background long job を開始し、すぐ `job_id` を返す |
| `agy_status` | job state を確認：running / completed / failed / cancelled / upstream_error |
| `agy_read` | job event stream を読む（raw / claude / codex protocols） |
| `agy_result` | finished job result を取得。`job_id` 省略時は最新の finished job を返す |
| `agy_cancel` | cross-platform process-group cancel |
| `agy_sessions` | 最近の session を一覧 |
| `agy_doctor` | env + auth + capability probe（secrets は出さない） |
| `agy_install_skill` | SKILL bundle を Claude / Codex / Antigravity dirs にインストール |
| `agy_purge` | local session-store directories を掃除（`days <= 0` は拒否） |

## いつ使うか / いつ使わないか

| Situation | Path |
|---|---|
| 現在の context だけで答えられる Q&A | 委譲せず、そのまま回答 |
| bug hypothesis への second opinion | `agy(..., mode="review")` |
| review 用 diff | `agy(..., mode="prototype")`（`allow_write` なし） |
| review 済み diff の適用 | `agy(..., mode="execute", allow_write=True)`（auto worktree） |
| 数時間規模の refactor | `agy_start(..., mode="long")` して poll |
| Anthropic / OpenAI conversation state が必要な作業 | 委譲しない。`agy` は独立 model / 独立 context |

## Safety floor

- すべての error / log / response field は `SafetyPolicy.redact` を通ります：
  `/Users/<u>/` → `~/`、PEM / JWT / AKID / Bearer / Authz は scrub されます。
- `mode=execute` の mutation には明示的な `allow_write=True` が必要です。
  destructive prompt は flag があっても拒否されます。
- `execute` mode は `~/.ssh`、`~/.aws/credentials`、browser cookie store、
  OS keychain を読んだり言及したりする prompt を拒否します。
- `mode=execute + allow_write` はデフォルトで `worktree=True`
  （`~/.config/agy-mcp/config.toml` または `AGY_MCP_WORKTREE_DEFAULT=0` で変更可能）。
- `~/.gemini/` には何も書きません（Antigravity CLI 自身の state dir）。
  user-scope antigravity SKILL は `~/.agy/skills/` に配置されます。

完全な threat model と明示的な「防御しないもの」一覧 →
[`security.md`](security.md)。

## Project-side snippets

repo の `CLAUDE.md` / `AGENTS.md` に配置できます：

- [`prompts/CLAUDE.md`](../prompts/CLAUDE.md) — Claude Code collaboration protocol
- [`prompts/AGENTS.md`](../prompts/AGENTS.md) — OpenAI Codex collaboration protocol
- [`prompts/antigravity-system.md`](../prompts/antigravity-system.md) — `agy` 側 system prompt の提案

## Documentation

| File | Contents |
|---|---|
| [`installation.md`](installation.md) | Install + Claude Code / Codex registration + SKILL + verification |
| [`architecture.md`](architecture.md) | Module map（caller / MCP server / bridge / supervisor / adapter / safety） |
| [`output-strategy.md`](output-strategy.md) | Hybrid backend：stdout + klog + transcript.jsonl + protocol translator |
| [`security.md`](security.md) | Threat model、defence catalogue、explicit non-defences |
| [`cli-capabilities.md`](cli-capabilities.md) | Live `agy --help` + capability matrix |
| [`examples.md`](examples.md) | 7 end-to-end scenarios |
| [`comparison-with-cli-wrappers.md`](comparison-with-cli-wrappers.md) | Stream-json passthrough vs Hybrid backend wrapper patterns |
| [`release.md`](release.md) | PyPI trusted publishing + GitHub Release manual（one-time setup + routine flow） |
| [`../CHANGELOG.md`](../CHANGELOG.md) | Version history（Keep a Changelog） |

## Development

```bash
uv sync
uv run pytest        # full test suite
uv run agymcp        # FastMCP stdio server（manual testing）
uv run agy-bridge --cd . --PROMPT "Hello" --mode ask --dry-run --debug
uv run agy-doctor    # environment and auth probe
```

## License

[MIT](../LICENSE).
