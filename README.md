# agy-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-462%20passed-brightgreen.svg)](#)
[![English](https://img.shields.io/badge/English-README-blue.svg)](docs/README_EN.md)

> **Skill-first, MCP-second** bridge from Claude Code / OpenAI Codex
> to **Google Antigravity CLI** (`agy`).
> 继承 [`upstream/reference`](https://example.invalid/upstream) 与
> [`upstream/reference`](https://example.invalid/upstream)
> 的工程经验，并在其基础上增加 capability detection、长任务异步、双 backend 路由
> （`agy` + `gemini-cli` 兼容回退）、安全策略、worktree 隔离、Codex / Antigravity skill、
> 跨平台 doctor、稳定的 stream-json 兼容事件 schema。

---

## 这是什么

把 Google 新发布的 Antigravity CLI（`agy`）包装成一个 **可被 Claude Code 或 OpenAI Codex
直接调用的协作 agent backend**：

- **Skill 优先**：通过 `~/.claude/skills/` / `~/.agents/skills/` 让 Claude / Codex 学会
  *什么时候* 调 `agy`、*怎么* 调；
- **MCP 次之**：通过 `agymcp` FastMCP server 暴露 9 个稳定 JSON 工具，调用方拿到的总是
  结构化 envelope；
- **安全为底**：所有错误、日志、响应字段都过一遍 `SafetyPolicy.redact`；写入操作默认走临时 git
  worktree；命令注入 / 路径穿越 / 父级 symlink 替换都有针对性防护。

除 `agy-doctor` 与 `--dry-run` 验证路径外，正常 `agy` / `agy_start`
调用会启动 `agy --print`，可能消耗真实 Antigravity 请求。本项目不重新实现
`agy` API，只做包装、路由、隔离、审计。

## 5 分钟上手

```bash
# 1. 安装 uv（如已安装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 安装 agy-mcp
uv tool install --from git+https://github.com/Boulea7/agy-mcp.git agy-mcp

# 3. 在 Claude Code 注册 MCP server
claude mcp add agy -s user --transport stdio -- agymcp

# 4. 安装协作 SKILL（Claude / Codex / Antigravity 三套）
agy-install-skill --target all

# 5. 验证（不调用真实 agy API）
agy-doctor
agy-bridge --cd . --PROMPT "Hello" --mode ask --dry-run --debug
```

完整安装与 Codex 配置参考 [`docs/installation.md`](docs/installation.md)。

## 9 个 MCP 工具

| 工具 | 用途 |
|---|---|
| `agy` | 同步一次性调用（兼容 upstream-reference 参数 + 新增 `mode` / `backend` / `output_protocol` / `worktree` / `allow_write` / `extra_env`） |
| `agy_continue` | 续 `SESSION_ID` |
| `agy_start` | 后台启动长任务，立即返回 `job_id` |
| `agy_status` | 查 job 状态：running / completed / failed / cancelled |
| `agy_read` | 读 job 事件流（raw / claude / codex 三种协议） |
| `agy_cancel` | 跨平台 process group 终止（POSIX `killpg` / Windows `CTRL_BREAK_EVENT`） |
| `agy_sessions` | 列最近 session（含 mtime / status / cwd 摘要） |
| `agy_doctor` | 环境 + 鉴权 + capability 探测（不泄漏 secrets） |
| `agy_install_skill` | 把 SKILL bundle 装到 Claude / Codex / Antigravity skill 目录 |

## 何时调用

| 任务类型 | 推荐路径 |
|---|---|
| 上下文窗口里能直接回答的 Q&A | 不调，自己答 |
| Bug 假设的第二意见 | `agy(..., mode="review")` |
| 给 review 用的 diff | `agy(..., mode="prototype")`（无 `allow_write`） |
| 应用 reviewed diff | `agy(..., mode="execute", allow_write=True)`（自动 worktree） |
| 数小时大重构 | `agy_start(..., mode="long")` 然后轮询 |

## 何时**不**调用

- 简单问题：来回开销不值。
- 需要实际 Anthropic / OpenAI 对话状态的：`agy` 是独立模型独立上下文。

## 安全底线

- 每条错误、日志、响应字段都先走 `SafetyPolicy.redact`：`/Users/<u>/` → `~/`，PEM / JWT /
  AKID / Bearer 全部脱敏；
- `mode=execute` 的写入必须显式 `allow_write=True`；即便置位，destructive prompt 仍会被拒；
- `execute` 模式下读 / 提及 `~/.ssh`、`~/.aws/credentials`、浏览器 cookie store、OS keychain
  都会被拒；
- `mode=execute --allow-write` 默认强制 worktree=true（可在 `~/.config/agy-mcp/config.toml`
  或 `AGY_MCP_WORKTREE_DEFAULT=0` 关闭）；
- 不写任何文件到 `~/.gemini/`（Antigravity CLI 自己的状态目录）；user-scope antigravity skill
  落在 wrapper 自有的 `~/.agy/skills/`。

完整威胁模型与「**不**防御**」清单：[`docs/security.md`](docs/security.md)。

## 项目协议片段

直接复制到你项目的 `CLAUDE.md` / `AGENTS.md`，告诉每一次 session 怎么用 `agy`：

- [`prompts/CLAUDE.md`](prompts/CLAUDE.md) — Claude Code 协作协议
- [`prompts/AGENTS.md`](prompts/AGENTS.md) — Codex 协作协议
- [`prompts/antigravity-system.md`](prompts/antigravity-system.md) — 给 `agy` 端的 system prompt
  建议

## 文档目录

| 文件 | 内容 |
|---|---|
| [`docs/installation.md`](docs/installation.md) | 安装 + Claude Code / Codex 注册 + SKILL 安装 + 验证 |
| [`docs/architecture.md`](docs/architecture.md) | 模块图（caller / MCP server / bridge / supervisor / adapter / safety） |
| [`docs/output-strategy.md`](docs/output-strategy.md) | Hybrid backend：stdout + klog + transcript.jsonl + 协议翻译器 |
| [`docs/security.md`](docs/security.md) | 威胁模型、防护清单、明确不防御项 |
| [`docs/cli-capabilities.md`](docs/cli-capabilities.md) | `agy --help` 实测 + capability 矩阵（CLI 升级时刷新） |
| [`docs/examples.md`](docs/examples.md) | 6 个典型场景：review、prototype、long、continue、doctor、install |
| [`docs/comparison-with-upstream-reference.md`](docs/comparison-with-upstream-reference.md) | 继承自 `upstream-reference` 的部分 + 扩展的部分 + 主动改的部分 |

英文版 README：[`docs/README_EN.md`](docs/README_EN.md)。

## 开发

```bash
# clone 后
uv sync
uv run pytest        # 全量 462 个测试
uv run agymcp        # 启动 MCP stdio server（人工测试用）
uv run agy-bridge --cd . --PROMPT "Hello" --mode ask --dry-run --debug
uv run agy-doctor    # 环境与鉴权探测
```

参考仓库放在 `.refs/`（gitignored）；运行 `git clone https://example.invalid/upstream
.refs/upstream-reference && git clone https://example.invalid/upstream
.refs/upstream-reference` 可拉下来对比。

## License

[MIT](LICENSE).
