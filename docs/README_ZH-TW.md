# agy-mcp (繁體中文)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](../pyproject.toml)
[![CI](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-560%20passed-brightgreen.svg)](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml)

語言：[`简体中文`](../README.md) · [`English`](README_EN.md) · [`日本語`](README_JA.md)

> 將 Google **Antigravity CLI**（`agy`）包裝成 11 個 typed MCP 工具，
> 任何 MCP client（Claude Code / OpenAI Codex / Cursor / Cline /
> Continue …）都能直接呼叫。專案也提供可選的 Skill bundle，讓支援
> skill 的平台知道*何時*委派、*使用哪個 mode*。

---

## 快速開始

```bash
# 1. 安裝 uv（已有可略過）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 安裝 agy-mcp（從 PyPI）
uv tool install agy-mcp

# 3. 註冊 MCP server（以 Claude Code 為例；其他 client 見下方摺疊區）
claude mcp add agy -s user --transport stdio -- agymcp

# 4. （可選）安裝 SKILL，讓 Claude / Codex / Antigravity 知道何時呼叫
agy-install-skill --target all

# 5. 驗證（不呼叫真實 agy API）
agy-doctor
```

<details>
<summary><strong>讓本機 agent 自己安裝</strong>（推薦：將下方提示詞貼到 Claude Code / OpenAI Codex CLI，它會自己讀取、執行、驗證）</summary>

````text
請幫我把 agy-mcp 這個開源 MCP 安裝好。具體目標：

1. 用 uv tool 安裝本倉庫的 console scripts（agymcp / agy-bridge /
   agy-doctor / agy-install-skill）。如果本機沒有 uv，先用
   `curl -LsSf https://astral.sh/uv/install.sh | sh` 安裝。
2. 把 agy MCP server 註冊到我目前正在使用的 agent 平台：
   - 如果你是 Claude Code，執行：
     `claude mcp add agy -s user --transport stdio -- agymcp`
     並用 `claude mcp list` 確認 `agy: ✓ Connected`。
   - 如果你是 OpenAI Codex CLI，向 `~/.codex/config.toml` 追加：
     ```toml
     [mcp_servers.agy]
     command = "agymcp"
     args = []
     ```
     並重新啟動 Codex session 讓它生效。
3. 安裝協作 SKILL（讓本平台知道何時呼叫 agy）：直接呼叫剛暴露的
   `agy_install_skill` MCP 工具，參數 `targets=["all"], scope="user"`。
   它會寫入 `~/.claude/skills/`、`~/.agents/skills/`、`~/.agy/skills/`
   三處。安裝後告訴我重新啟動視窗讓 SKILL 生效。
4. 驗證：呼叫 `agy_doctor` MCP 工具，把回傳的 JSON 完整貼給我看；
   預期 `healthy=true`，主要 check 都是 `ok=true`。如果 `auth` 顯示
   未登入，告訴我手動執行一次 `agy --version` 觸發 OAuth 流程，
   然後再呼叫一次 `agy_doctor`。
5. 安裝來源預設使用 PyPI：`uv tool install agy-mcp`。如果我要求安裝
   未發布分支或本機 clone，再切換到
   `git+https://github.com/Boulea7/agy-mcp.git` 或本機
   `--from <專案本機絕對路徑>`。

約束：
- 不要 push 任何內容，也不要修改我的 .gitconfig。
- 在沒有我確認前不要呼叫真實的 `agy` 模型（只能跑 doctor / dry-run）。
- 任何寫入操作做之前先簡短說明要做什麼，等我同意再執行；如果你目前
  的權限模式允許 acceptEdits，就直接執行。

每完成一步給我一行回報。全部完成後給出 4 行總結：安裝位置、11 個
MCP 工具是否齊全、SKILL 落地路徑、剩餘可選項。
````

</details>

<details>
<summary><strong>其他 MCP client 註冊方式</strong></summary>

- **OpenAI Codex CLI**：向 `~/.codex/config.toml` 追加：
  ```toml
  [mcp_servers.agy]
  command = "agymcp"
  args = []
  ```
  重新啟動 Codex session 後生效。
- **Cursor / Cline / Continue / 其他 MCP client**：在 client 的
  MCP server 設定裡新增 name=`agy`、command=`agymcp`、transport=stdio。
  各家的具體語法不同，請參考各自文件。

</details>

完整安裝與疑難排解 → [`installation.md`](installation.md)。

---

## 它是什麼

這是一個 wrapper，將 Google 新推出的 Antigravity CLI（`agy`）變成任意
MCP client 都能呼叫的協作 agent backend。它提供兩條等價路徑：

- **MCP server**：`agymcp` 透過 FastMCP stdio 暴露 11 個 typed JSON
  工具，pydantic envelope 穩定可解析。**任何 MCP client 都可使用**。
- **Skill bundles**：安裝到 `~/.claude/skills/`、`~/.agents/skills/`、
  `~/.agy/skills/`，教 agent *何時*呼叫 agy、*使用哪個 mode*、遵守
  哪些安全規則。**只對 Claude Code / OpenAI Codex / Antigravity 有效**。
- **共享 backend**：兩條路徑都走同一套 `bridge.py` → adapter →
  safety policy → worktree，因此行為一致。

> 除 `agy_doctor` 與 `--dry-run` 外，`agy` / `agy_start` 會啟動真實
> `agy --print`，可能消耗 Antigravity 請求額度。本專案只負責包裝、
> 路由、隔離與審計，不重新實作 `agy` API。

## 11 個 MCP 工具

| 工具 | 用途 |
|---|---|
| `agy` | 同步一次性呼叫（PROMPT / cd / sandbox / SESSION_ID + `mode` / `backend` / `output_protocol` / `worktree` / `allow_write` / `extra_env`） |
| `agy_continue` | 延續既有 `SESSION_ID` |
| `agy_start` | 啟動背景長任務，立即回傳 `job_id` |
| `agy_status` | 查詢 job 狀態：running / completed / failed / cancelled / upstream_error；`job_id` 可使用唯一前綴 |
| `agy_read` | 讀取 job 事件流（raw / claude / codex 三種協定）；`job_id` 可使用唯一前綴 |
| `agy_result` | 取得已完成 job 的結果；不傳 `job_id` 時回傳最近完成任務；傳參可使用唯一前綴 |
| `agy_cancel` | 跨平台 process group 終止；`job_id` 可使用唯一前綴 |
| `agy_sessions` | 列出最近 session |
| `agy_doctor` | 環境 + 鑑權 + capability 探測（不洩漏 secrets） |
| `agy_install_skill` | 將 SKILL bundle 安裝到 Claude / Codex / Antigravity 目錄 |
| `agy_purge` | 清理本機 session-store 目錄（拒絕 `days <= 0`） |

## 何時呼叫 / 何時不要呼叫

| 情境 | 建議路徑 |
|---|---|
| 目前上下文可直接回答的 Q&A | 不委派，直接回答 |
| Bug 假設的第二意見 | `agy(..., mode="review")` |
| 給 review 用的 diff | `agy(..., mode="prototype")`（不帶 `allow_write`） |
| 套用已 review 的 diff | `agy(..., mode="execute", allow_write=True)`（自動 worktree） |
| 數小時的大型重構 | `agy_start(..., mode="long")` 後輪詢 |
| 需要 Anthropic / OpenAI 對話狀態 | 不委派，`agy` 是獨立模型與獨立上下文 |

## 安全底線

- 所有錯誤 / 日誌 / 回應欄位都會先經過 `SafetyPolicy.redact`：
  `/Users/<u>/` → `~/`，PEM / JWT / AKID / Bearer / Authz 會被脫敏。
- `mode=execute` 寫入必須明確設定 `allow_write=True`；destructive prompt
  即使設定該旗標也會被拒絕。
- `execute` mode 會拒絕讀取或提及 `~/.ssh`、`~/.aws/credentials`、
  瀏覽器 cookie、OS keychain 的 prompt。
- `mode=execute + allow_write` 預設 `worktree=True`（可透過
  `~/.config/agy-mcp/config.toml` 或 `AGY_MCP_WORKTREE_DEFAULT=0` 關閉）。
- 不向 `~/.gemini/` 寫任何檔案（該目錄屬於 Antigravity CLI 自身狀態）；
  user-scope antigravity SKILL 會落在 `~/.agy/skills/`。

完整威脅模型與「**不**防禦」清單 → [`security.md`](security.md)。

## 專案端提示片段

可以直接放到專案根目錄的 `CLAUDE.md` / `AGENTS.md`：

- [`prompts/CLAUDE.md`](../prompts/CLAUDE.md) — Claude Code 協作協定
- [`prompts/AGENTS.md`](../prompts/AGENTS.md) — OpenAI Codex 協作協定
- [`prompts/antigravity-system.md`](../prompts/antigravity-system.md) — `agy` 端 system prompt 建議

## 文件目錄

| 檔案 | 內容 |
|---|---|
| [`installation.md`](installation.md) | 安裝 + Claude Code / Codex 註冊 + SKILL + 驗證 |
| [`architecture.md`](architecture.md) | 模組圖（caller / MCP server / bridge / supervisor / adapter / safety） |
| [`output-strategy.md`](output-strategy.md) | Hybrid backend：stdout + klog + transcript.jsonl + protocol translator |
| [`security.md`](security.md) | 威脅模型、防護清單、明確不防禦項 |
| [`cli-capabilities.md`](cli-capabilities.md) | `agy --help` 實測 + capability 矩陣 |
| [`examples.md`](examples.md) | 7 個典型場景 |
| [`comparison-with-cli-wrappers.md`](comparison-with-cli-wrappers.md) | Stream-json passthrough vs Hybrid backend 兩種 wrapper 模式對比 |
| [`release.md`](release.md) | PyPI trusted publishing + GitHub Release 發布手冊（一次性設定 + 常規流程） |
| [`../CHANGELOG.md`](../CHANGELOG.md) | 版本變更記錄（Keep a Changelog） |

## 開發

```bash
uv sync
uv run pytest        # 全量測試
uv run agymcp        # 啟動 FastMCP stdio server（人工測試用）
uv run agy-bridge --cd . --PROMPT "Hello" --mode ask --dry-run --debug
uv run agy-doctor    # 環境與鑑權探測
```

## License

[MIT](../LICENSE).
