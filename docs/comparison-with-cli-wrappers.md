# CLI wrapper 设计模式对比

> 本文讨论把 LLM CLI 包装成 MCP server 的两种主流设计模式，
> 帮助选择适合自己工具链的实现路径。**零具名引用** —— 抽象讨论
> 设计抉择，不点名上游。

## 两种模式

### A. Stream-json passthrough

**前提**：上游 CLI 原生支持 `--output-format stream-json`（或等价
的 SSE / NDJSON 流），每个 token / tool call / lifecycle 事件都已
是结构化 JSON。

**实现**：MCP server 调 CLI，把 stdout 行一行行直接吐回给 caller。
基本上是 100 行 Python 的 subprocess wrapper。

### B. Hybrid backend（agy-mcp 模式）

**前提**：上游 CLI **只输出 plain text/markdown**，无原生流式
JSON，但有侧通道日志（`--log-file` / `~/.cache/<cli>/` / 子进程
NDJSON）。

**实现**：三路并发合成事件流：
- stdout reader：缓冲全部 chunk，进程结束时一次 emit `assistant`
  event。
- 侧通道 tail（如 klog 文件）：正则解析生命周期事件，emit
  `conversation_started` / `turn_start` / `turn_end` / `error`。
- 可选 transcript watcher：NDJSON 透传为 `subagent_event`。

详见 [`output-strategy.md`](output-strategy.md)。

## 设计差异

| 维度 | A. Stream-json passthrough | B. Hybrid backend |
|---|---|---|
| 上游 CLI 要求 | 必须原生支持流式 JSON | 只要有 stdout + 侧通道日志 |
| Session 锚点 | 上游 stream metadata 给 | 从日志正则抽 conversation_id |
| 工具数量 | 通常 1-2 个（透传） | 多个细化的生命周期工具 |
| 长任务支持 | 取决于上游 CLI | 内置 supervisor + job_id + cancel |
| 安全策略 | 取决于调用方 | 内置 secret redaction + worktree isolation |
| Skill bundle | 通常无 | 多平台 Skill 教调用时机 |
| Worktree 隔离 | 通常无 | execute+allow_write 默认 `worktree=True` |
| Doctor 探测 | 通常无 | capability + auth + 环境 check |
| 实现复杂度 | 低 | 中-高 |
| 维护成本 | 低 | 中（要跟踪上游 CLI 日志格式变化） |

## 什么时候用哪种

### 选 A（passthrough）
- 上游 CLI 原生 stream-json 体验完整。
- 用户主要诉求是"跑一次命令拿结果"。
- 不想维护额外的 supervisor / safety / worktree 逻辑。
- 上游 CLI 自己已有强 session / cancel / auth 处理。

### 选 B（hybrid backend，agy-mcp 模式）
- 上游 CLI 只出 plain text，但你需要给 agent 提供生命周期事件。
- 上层 agent 需要 session resume、长任务后台运行、cancel。
- 跨多个 MCP 客户端服务（需要稳定 typed envelope）。
- 业务有 review-then-apply 工作流，需要 `mode` 概念
  (`review` / `prototype` / `execute`)。
- 要有内置 safety floor（destructive prompt 拒绝、write
  路径 sanity check）。

## 迁移触发点

正在用 A 模式但下列场景出现时，考虑迁移到 B 模式：

| 现象 | B 模式提供的能力 |
|---|---|
| 上游 CLI 经常 stale 或不响应，用户要"先丢着，回头看结果" | supervisor 长任务 + job_id + 跨平台 cancel |
| 用户反馈"不知道什么时候该调你的 wrapper" | Skill bundle 教"何时调 / 用哪个 mode" |
| 业务有"先生成 diff 给我看，再决定要不要应用" | mode 概念（prototype 出 diff、execute 应用） |
| 调用方 paste 日志到 Slack 被审计扣分 | SafetyPolicy.redact 自动脱敏 PEM / JWT / AKID |
| 大重构期间，上游 CLI 误改主分支 | worktree 隔离，写入默认落 `.agy-mcp/worktrees/` |

不强求一次迁完 —— B 模式所有能力都是 opt-in：用户用
`agy(PROMPT, ...)` 还是简单调用，supervisor / worktree /
Skill 只在显式开启时介入。

## 不变量（两种模式都必须守）

无论 A 还是 B，下面两条不能违反：

1. **事件 envelope 稳定**：上游 CLI 升级不应该让 caller 接收的
   JSON shape 变化。A 模式靠 schema version 锁，B 模式靠
   adapter layer 把上游行为变化吸收在内部。
2. **secret redaction 是 wrapper 责任**：所有错误 / 日志 / 响应
   字段必经过 redact 才能 leak 出去。**不能假设上游 CLI 不会回
   secret**（它经常会回 stack trace 里的环境变量）。

## 相关文档

- [`output-strategy.md`](output-strategy.md) — Hybrid backend 的
  三路事件流实现细节。
- [`architecture.md`](architecture.md) — agy-mcp 完整模块图。
- [`security.md`](security.md) — SafetyPolicy 设计与威胁模型。
