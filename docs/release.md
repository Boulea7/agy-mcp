# 发布手册（PyPI + GitHub Release）

`agy-mcp` 使用 **PyPI trusted publishing** + **GitHub Actions OIDC**
发布。本地不需要、也**不应该**配置 PyPI API token；所有 publish
都走 OIDC 短期凭证。

## 一次性设置（首次发布前）

### 1. 在 GitHub 仓库建 environment

`Settings → Environments → New environment`，名字必须是 `release`
（与 `.github/workflows/release.yml` 里的 `environment.name` 一致）。

可选保护：
- **Required reviewers**：配置后 publish job 需要批准；是否允许
  自己批准取决于 environment 的自审设置。
- **Wait timer**：按需延迟 publish job，等待期间可取消 workflow；
  这不能撤回已完成的上传。
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

### 3. 发布前本地验证

`Release` 的 `workflow_dispatch` **会真实发布**：它从指定 tag
运行 verify、build、PyPI publish 和 GitHub Release，没有 dry-run
开关。不要用已发布的 tag 试跑，也不要临时改 workflow 做 TestPyPI
验证。手动重跑时，workflow 的运行 ref 和 `tag` 输入必须指定同一个
版本 tag（例如 `v0.1.9`）。在上述 `v*` tag 限制下，仅填写 `tag`
输入而从 `main` 运行 workflow，不能满足 environment 的发布规则。
发布前先在本地核对 `pyproject.toml`、`src/agy_mcp/__init__.py`
与 `uv.lock` 的项目版本以及 CHANGELOG，并在空的 `dist/` 目录中构建：

```bash
uv run ruff check src tests scripts && \
uv run pytest -q && \
uv build && \
uv run python scripts/check_release_artifacts.py
```

用全新虚拟环境安装刚构建的 wheel，避免旧 editable 安装掩盖漏文件。
以下离线示例要求提前准备好所有依赖 wheel；缺包时先补齐本地依赖，
不要把安装失败当验证成功：

```bash
smoke_dir="$(mktemp -d)" && \
python3 -m venv "$smoke_dir/venv" && \
"$smoke_dir/venv/bin/python" -m pip --isolated install --no-index \
  --find-links /path/to/dependency-wheels --retries 0 \
  dist/agy_mcp-0.1.9-py3-none-any.whl && \
"$smoke_dir/venv/bin/python" -I - <<'PY'
from importlib import import_module
from importlib.metadata import distribution

dist = distribution("agy-mcp")
assert dist.version == "0.1.9"
expected = {
    "agymcp": "agy_mcp.cli:main",
    "agy-bridge": "agy_mcp.bridge:main",
    "agy-doctor": "agy_mcp.doctor:main",
    "agy-install-skill": "agy_mcp.install:main",
}
assert {ep.name: ep.value for ep in dist.entry_points
        if ep.group == "console_scripts"} == expected
import_module("agy_mcp.server")
print("Installed wheel metadata and server import OK")
PY
```

这里仅检查安装元数据与模块导入，不调用 console 入口或 MCP 工具。
`agymcp --help` 会启动 stdio server；`agy-bridge --dry-run` 会探测
真实后端与鉴权；`agy-doctor` 会读取本机鉴权状态；`agy-install-skill`
默认写入用户目录。它们属于用户自愿执行的手工诊断或安装操作，不能
作为无副作用的自动 smoke，也不能代替真实 provider E2E 验证。

## 常规发布流程

```bash
# 1. Verify version 0.1.9, CHANGELOG, the release commit, and a clean checkout.
git status --short  # Must be empty.
git log --oneline -3
# Complete the local checks above and verify the release commit SHA.

# 2. Tag the verified release commit; confirm this version is not already published.
# 3. Push the annotated tag only if tag creation succeeds.
git tag -a v0.1.9 -m "release: v0.1.9 — <one-line summary>" && \
git push origin v0.1.9

# 4. Check the Release workflow in GitHub Actions.
#    https://github.com/Boulea7/agy-mcp/actions/workflows/release.yml
#    a) verify (matrix tests) → build (audit) → publish
#       Approval is required only if configured for the release environment.
#    b) publish uses OIDC trusted publishing to PyPI.
#    c) github-release attaches the wheel and sdist with generated release notes.
```

## 发布后验证

```bash
# Check the published PyPI version.
curl -s https://pypi.org/pypi/agy-mcp/json | jq '.info.version'

# Install the exact PyPI version in another fresh environment.
smoke_dir="$(mktemp -d)" && \
python3 -m venv "$smoke_dir/venv" && \
"$smoke_dir/venv/bin/python" -m pip --isolated install --retries 0 agy-mcp==0.1.9
```

再用该环境的 Python 执行上面的 metadata/import 检查。不要调用四个
console 入口来代替安装验证；真实后端与平台集成仍需单独手工验证。

## 回滚

PyPI **不允许 delete + 重发同一版本号**（即便 yank 也只是隐藏）。
出问题立即：

1. **小问题**：发新的 patch 版本，CHANGELOG 注明
   "supersedes v0.1.9 due to <issue>"。
2. **严重问题（数据丢失 / 安全）**：
   - PyPI 上 yank v0.1.9（标记为不可见，`pip install agy-mcp`
     不再选它，但 `agy-mcp==0.1.9` 仍可装）。
   - 发 patch 版本修复 + 公告。

## 常见坑

- **PyPI 端 trusted publisher 没建** → `publish` job 报
  `invalid-publisher: valid token, but no corresponding publisher`。
  按 §1 步骤建一遍。
- **Environment 名字拼错** → trusted publisher 校验可能失败。
  检查 PyPI publisher config 里的 environment
  name 与 release.yml 一字不差（区分大小写）。
- **版本、CHANGELOG 或 tag 指向的提交不一致** → 可能发布错误产物。
  push tag 前逐项核对；GitHub Release notes 由 workflow 自动生成。
- **`uv build` 漏文件** → release-gate 会 fail。原因通常是
  `pyproject.toml` 的 `[tool.hatch.build.targets.wheel]` /
  `[tool.hatch.build.targets.sdist]` 没把新模块加进去；同时
  `scripts/check_release_artifacts.py` 的 `REQUIRED_*_FILES` 也
  要更新。

## Trusted publishing 的优势

- 无需本地 PyPI API token：publish 通过 OIDC 换取临时发布凭证，
  不假定固定有效期。
- 发布身份受 GitHub repo、workflow 与 environment 配置约束。
- 可审计：每次 publish 都有 GitHub Actions log + PyPI 端 publisher
  log 双向追溯。
- 仓库、workflow 或 environment 改名时需要同步核对 publisher 配置。
