# 发布手册（PyPI + GitHub Release）

`agy-mcp` 使用 **PyPI trusted publishing** + **GitHub Actions OIDC**
发布。本地不需要、也**不应该**配置 PyPI API token；所有 publish
都走 OIDC 短期凭证。

## 一次性设置（首次发布前）

### 1. 在 GitHub 仓库建 environment

`Settings → Environments → New environment`，名字必须是 `release`
（与 `.github/workflows/release.yml` 里的 `environment.name` 一致）。

可选保护：
- **Required reviewers**：勾选自己 — 每次发布前你点一下 Approve
  才会真正 publish，给一个最后的人工 abort 机会。
- **Wait timer**：5 min — push tag 后 5 分钟内可撤销。
- **Deployment branches**：限制为 tag `v*` —— 防止有人推个野
  branch 触发发布。

### 2. 在 PyPI 配 trusted publisher

去 https://pypi.org → Account → Publishing → Add a new pending
publisher（如果 `agy-mcp` 还未存在）/ Manage publishers（如果已
首次发过）。

填写：
- **PyPI Project Name**：`agy-mcp`
- **Owner**：`Boulea7`
- **Repository name**：`agy-mcp`
- **Workflow filename**：`release.yml`
- **Environment name**：`release`

确认后 PyPI 端会信任本仓库的 `release.yml` workflow 在
`release` environment 下发起的 publish 请求。**不需要任何
token、密码或 API key。**

### 3. 验证

跑一次 `.github/workflows/release.yml` 的 `workflow_dispatch`，
input 填一个已存在的 tag（如 `v0.1.5`）做 dry-run。如果你不想
实际 publish，先临时把 release.yml 里的 publish job 的
`pypa/gh-action-pypi-publish` 改成 `--repository-url
https://test.pypi.org/legacy/`（TestPyPI），并在 TestPyPI 也
配同样的 trusted publisher。

## 常规发布流程

```bash
# 1. 确认 CHANGELOG.md 已写好新版本 entry，main 干净
git status --short  # 必须空
git log --oneline -3

# 2. 打 annotated tag（必须 annotated，否则 GH Release notes 抓不到）
git tag -a v0.1.6 -m "release: v0.1.6 — <one-line summary>"

# 3. push tag 触发 release workflow
git push origin v0.1.6

# 4. 去 GitHub Actions 看 Release workflow
#    https://github.com/Boulea7/agy-mcp/actions/workflows/release.yml
#    a) verify (matrix tests) → build (audit) → 等 Approve
#    b) Approve 后 publish 跑 OIDC trusted publishing 到 PyPI
#    c) github-release 创建 GitHub Release，附 wheel + sdist + 自动 release notes
```

## 发布后验证

```bash
# PyPI 上能搜到
curl -s https://pypi.org/pypi/agy-mcp/json | jq '.info.version'

# 用 uvx 直接调（无需 git clone）
uvx --from agy-mcp agymcp --help

# uv tool install 切到 PyPI 源
uv tool uninstall agy-mcp
uv tool install agy-mcp
agymcp --help
agy-doctor
```

## 回滚

PyPI **不允许 delete + 重发同一版本号**（即便 yank 也只是隐藏）。
出问题立即：

1. **小问题**：发 patch 版本 `v0.1.6` → `v0.1.7`，CHANGELOG 注明
   "supersedes v0.1.6 due to <issue>"。
2. **严重问题（数据丢失 / 安全）**：
   - PyPI 上 yank v0.1.6（标记为不可见，`pip install agy-mcp`
     不再选它，但 `agy-mcp==0.1.6` 仍可装）。
   - 发 patch 版本修复 + 公告。

## 常见坑

- **PyPI 端 trusted publisher 没建** → `publish` job 报
  `invalid-publisher: valid token, but no corresponding publisher`。
  按 §1 步骤建一遍。
- **Environment 名字拼错** → workflow 卡在 `Waiting for review`
  永不进入 publish。检查 PyPI publisher config 里的 environment
  name 与 release.yml 一字不差（区分大小写）。
- **CHANGELOG 没写 / tag 不是 annotated** → GH Release notes 抓
  不到摘要，会用 GitHub 自动生成的 "compare since last tag" 文案
  （勉强可用但不如手工写）。
- **`uv build` 漏文件** → release-gate 会 fail。原因通常是
  `pyproject.toml` 的 `[tool.hatch.build.targets.wheel]` /
  `[tool.hatch.build.targets.sdist]` 没把新模块加进去；同时
  `scripts/check_release_artifacts.py` 的 `REQUIRED_*_FILES` 也
  要更新。

## Trusted publishing 的优势

- 无长期凭证：每次 publish 由 OIDC 临时签发，10 分钟过期。
- 凭证泄漏不可重放：与 GitHub repo + workflow + environment 三元组
  绑定，复制走也用不了。
- 可审计：每次 publish 都有 GitHub Actions log + PyPI 端 publisher
  log 双向追溯。
- 设置一次永久有效：对比经典 API token "上传一个 secret + 6 个月
  rotate"，trusted publishing 一次配完无需维护。
