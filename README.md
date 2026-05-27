# agy-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![CI](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-540%20passed-brightgreen.svg)](https://github.com/Boulea7/agy-mcp/actions/workflows/ci.yml)
[![English](https://img.shields.io/badge/English-README-blue.svg)](docs/README_EN.md)

> 把 Google **Antigravity CLI**（`agy`）包装成 11 个 typed MCP 工具，
> 任何 MCP 客户端（Claude Code / OpenAI Codex / Cursor / Cline /
> Continue …）都能直接调用。配套可选 Skill bundle，让支持 skill 的
> 平台学会*何时*调、*用哪个 mode*。

---

## 快速开始

```bash
# 1. 装 uv（已有可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 装 agy-mcp（从 PyPI）
uv tool install agy-mcp

# 3. 注册 MCP server（以 Claude Code 为例；其它客户端见折叠区）
claude mcp add agy -s user --transport stdio -- agymcp

# 4. （可选）装 SKILL，让 Claude / Codex / Antigravity 学会何时调
agy-install-skill --target all

# 5. 验证（不调真实 agy API）
agy-doctor
```

<details>
<summary><strong>让本地 agent 自己装</strong>（推荐：复制下方提示词到 Claude Code / OpenAI Codex CLI，它会自己读、执行、验证）</summary>

````text
请帮我把 agy-mcp 这个开源 MCP 安装好。具体目标：

1. 用 uv tool 装本仓库的 console scripts（agymcp / agy-bridge /
   agy-doctor / agy-install-skill）。如果本机没有 uv，先用
   `curl -LsSf https://astral.sh/uv/install.sh | sh` 装好。
2. 把 agy MCP server 注册到我当前正在使用的 agent 平台：
   - 如果你是 Claude Code，执行：
     `claude mcp add agy -s user --transport stdio -- agymcp`
     并用 `claude mcp list` 确认 `agy: ✓ Connected`。
   - 如果你是 OpenAI Codex CLI，向 `~/.codex/config.toml` 追加：
     ```toml
     [mcp_servers.agy]
     command = "agymcp"
     args = []
     ```
     并重启 Codex 会话让其生效。
3. 装协作 SKILL（让本平台知道何时调用 agy）：直接调用刚才暴露的
   `agy_install_skill` MCP 工具，参数 `targets=["all"], scope="user"`。
   它会写到 `~/.claude/skills/`、`~/.agents/skills/`、`~/.agy/skills/`
   三处。装完后告诉我重启窗口让 SKILL 生效。
4. 验证：调用 `agy_doctor` MCP 工具，把返回的 JSON 完整贴给我看；
   预期 `healthy=true`，6 项 check 全部 `ok=true`。如果 `auth` 这项
   显示未登录，告诉我手动跑一次 `agy --version` 触发 OAuth 流程，
   然后再调一次 `agy_doctor`。
5. 安装来源默认走 PyPI：`uv tool install agy-mcp`。如果我让你装某个
   未发布分支或本地 clone，再切到 `git+https://github.com/Boulea7/agy-mcp.git`
   或本地 `--from <项目本地绝对路径>`。

约束：
- 不要 push 任何东西、不要改我的 .gitconfig。
- 在没有我确认前不要调真实的 `agy` 模型（只能跑 doctor / dry-run）。
- 任何写入操作（包括上面这些）做之前先简短说一下要做什么，等我点头
  再执行；如果你的当前权限模式允许 acceptEdits，就直接执行。

每完成一步给我一行汇报，全部完成后给出一份 4 行总结：装在哪、11 个
MCP 工具是否齐、SKILL 落地路径、剩余可选项。
````

</details>

<details>
<summary><strong>其它 MCP 客户端注册方式</strong></summary>

- **OpenAI Codex CLI**：向 `~/.codex/config.toml` 追加：
  ```toml
  [mcp_servers.agy]
  command = "agymcp"
  args = []
  ```
  重启 Codex 会话生效。
- **Cursor / Cline / Continue / 其它 MCP 客户端**：在客户端的
  MCP server 配置里加一条 name=`agy`、command=`agymcp`、transport=stdio
  即可。具体语法各家不同，参考各自文档。

</details>

完整安装与故障排查 → [`docs/installation.md`](docs/installation.md)。

---

## 它是什么

把 Google 新发布的 Antigravity CLI（`agy`）包装成可被任意 MCP 客户端
调用的协作 agent backend。两条等价路径：

- **MCP server**：`agymcp` 经 FastMCP stdio 暴露 11 个 typed JSON 工具，
  pydantic envelope 稳定可解析。**任何 MCP 客户端皆可**。
- **Skill bundles**：装到 `~/.claude/skills/`、`~/.agents/skills/`、
  `~/.agy/skills/`，教 agent *何时*调 agy、*用哪个 mode*、注意哪些安全
  规则。**仅对 Claude Code / OpenAI Codex / Antigravity 三家有效**。
- **共享 backend**：两条路径都走同一 `bridge.py` → adapter → safety
  policy → worktree，行为一致。

> 除 `agy_doctor` 与 `--dry-run` 外，`agy` / `agy_start` 会启动真实
> `agy --print`，可能消耗 Antigravity 请求额度。本项目只包装、路由、
> 隔离、审计，不重新实现 `agy` API。

## 11 个 MCP 工具

| 工具 | 用途 |
|---|---|
| `agy` | 同步一次性调用（PROMPT / cd / sandbox / SESSION_ID + `mode` / `backend` / `output_protocol` / `worktree` / `allow_write` / `extra_env`） |
| `agy_continue` | 续 `SESSION_ID` |
| `agy_start` | 后台启动长任务，立即返回 `job_id` |
| `agy_status` | 查 job 状态：running / completed / failed / cancelled / upstream_error |
| `agy_read` | 读 job 事件流（raw / claude / codex 三协议） |
| `agy_result` | 取已完成 job 的结果；不传 `job_id` 时返回最近完成任务 |
| `agy_cancel` | 跨平台 process group 终止 |
| `agy_sessions` | 列最近 session |
| `agy_doctor` | 环境 + 鉴权 + capability 探测（不泄漏 secrets） |
| `agy_install_skill` | 把 SKILL bundle 装到 Claude / Codex / Antigravity 目录 |
| `agy_purge` | 清理本机 session-store 目录（refuse `days<=0`） |

## 何时调用 / 何时不调用

| 情景 | 建议路径 |
|---|---|
| 上下文里能直接答的 Q&A | 不调，自己答 |
| Bug 假设的第二意见 | `agy(..., mode="review")` |
| 给 review 用的 diff | `agy(..., mode="prototype")`（无 `allow_write`） |
| 应用 reviewed diff | `agy(..., mode="execute", allow_write=True)`（自动 worktree） |
| 数小时大重构 | `agy_start(..., mode="long")` 然后轮询 |
| 需要 Anthropic / OpenAI 对话状态 | 不调 —— `agy` 是独立模型独立上下文 |

## 安全底线

- 所有错误 / 日志 / 响应字段先过 `SafetyPolicy.redact`：`/Users/<u>/` → `~/`，PEM / JWT / AKID / Bearer 全脱敏；
- `mode=execute` 写入必须显式 `allow_write=True`；destructive prompt 即使置位仍拒；
- `execute` 模式下读 / 提及 `~/.ssh`、`~/.aws/credentials`、浏览器 cookie、OS keychain 都拒；
- `mode=execute + allow_write` 默认 `worktree=True`（可经 `~/.config/agy-mcp/config.toml` 或 `AGY_MCP_WORKTREE_DEFAULT=0` 关闭）；
- 不写任何文件到 `~/.gemini/`（Antigravity CLI 自有状态目录）；user-scope antigravity skill 落在 `~/.agy/skills/`。

完整威胁模型与「**不**防御」清单 → [`docs/security.md`](docs/security.md)。

## 项目协议片段

直接复制到项目根的 `CLAUDE.md` / `AGENTS.md`：

- [`prompts/CLAUDE.md`](prompts/CLAUDE.md) — Claude Code 协作协议
- [`prompts/AGENTS.md`](prompts/AGENTS.md) — OpenAI Codex 协作协议
- [`prompts/antigravity-system.md`](prompts/antigravity-system.md) — `agy` 端 system prompt 建议

## 文档目录

| 文件 | 内容 |
|---|---|
| [`docs/installation.md`](docs/installation.md) | 安装 + Claude Code / Codex 注册 + SKILL + 验证 |
| [`docs/architecture.md`](docs/architecture.md) | 模块图（caller / MCP server / bridge / supervisor / adapter / safety） |
| [`docs/output-strategy.md`](docs/output-strategy.md) | Hybrid backend：stdout + klog + transcript.jsonl + 协议翻译器 |
| [`docs/security.md`](docs/security.md) | 威胁模型、防护清单、明确不防御项 |
| [`docs/cli-capabilities.md`](docs/cli-capabilities.md) | `agy --help` 实测 + capability 矩阵 |
| [`docs/examples.md`](docs/examples.md) | 7 个典型场景 |
| [`docs/comparison-with-cli-wrappers.md`](docs/comparison-with-cli-wrappers.md) | Stream-json passthrough vs Hybrid backend 两种 wrapper 模式对比 |
| [`docs/release.md`](docs/release.md) | PyPI trusted publishing + GitHub Release 发布手册（一次性设置 + 常规流程） |
| [`CHANGELOG.md`](CHANGELOG.md) | 版本变更记录（Keep a Changelog） |

英文版 README → [`docs/README_EN.md`](docs/README_EN.md)。

## 开发

```bash
uv sync
uv run pytest        # 全量 525 个测试
uv run agymcp        # 启动 MCP stdio server（人工测试用）
uv run agy-bridge --cd . --PROMPT "Hello" --mode ask --dry-run --debug
uv run agy-doctor    # 环境与鉴权探测
```

## License

[MIT](LICENSE).
