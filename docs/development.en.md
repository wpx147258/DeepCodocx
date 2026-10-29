<!-- Generated language panels. Edit this file's default-open language, then run python scripts/sync_doc_languages.py. -->

<details name="deepcodocx-doc-language" lang="zh-CN">
<summary>简体中文</summary>

<!-- doc-language:zh-CN:start -->
# 开发指南

[首页](../README.md)

## 环境与检查

```bash
uv sync --extra dev
uv run ruff check src tests scripts
uv run mypy src
uv run pytest -q --cov=dcx --cov-report=term-missing --cov-fail-under=75
node --check src/dcx/web/static/app.js
```

源码包为 `src/dcx`，安装后的命令是 `cli-agent`。CLI、Web 和 MCP 子进程均使用此包。
模块边界见[架构说明](architecture.md)，开发约定见 [AGENTS.md](../AGENTS.md)。

每个新增行为应有测试。Loop 测试使用 `tests/support/fake_provider.py`；文档测试通过
微型 basic / Marker fixtures 运行，不依赖个人语料或 API key。

## 浏览器验收

```bash
uv run --with playwright python -m playwright install chromium
uv run --with playwright python -m tests.e2e.web_smoke --output /tmp/deepcodocx-browser
```

该验收使用脚本 provider，运行真实 Web 服务、stdio MCP 和浏览器，检查文档库默认值、
选择记忆、fork/new、搜索、图片、引用、会话隔离及窄屏布局。
输出目录中的截图用于界面核对。附加 `--library /path/to/paper-bundles` 可检查包含
MEMORYVLA Marker 产物的真实本地语料，目录只读访问。

## 真实 provider 测试

```bash
export CLI_AGENT_E2E_PROVIDER=deepseek
export DEEPSEEK_API_KEY="your-api-key"
uv run pytest tests/e2e -q
```

这组测试会请求真实服务；未设置 provider 时默认跳过。模型可通过
`CLI_AGENT_E2E_MODEL` 指定。CI 的手动 `workflow_dispatch` 任务使用对应仓库 secret。

## 贡献方向

- 维持唯一 Agent loop，具体对象集中在 assembly 装配。
- 新工具声明真实 schema 与权限分类；只有明确安全的工具才允许并发。
- 新 provider 只在 adapter 中处理 wire format，内部消息保持中立。
- 维护 JSONL / SSE 的公开字段与完成语义，stdout 与诊断分离。
- 文档内容按不可信数据处理；外部工具不能自行授予权限。
- 检查恢复、取消、异常与资源释放路径，同时保持旧会话记录兼容。

仓库公开文档维护产品使用、配置、架构和贡献说明。个人计划、学习笔记与审查过程保存在
Git 忽略的本地目录，不加入发布提交。

## 双语文档

GitHub 文档使用同名 `<details>` 互斥组切换语言。点击语言标题会在当前页展开对应正文并
收起另一种语言，不跳转页面，也不执行 JavaScript。

修改中文内容时，编辑 `.md` 文件中默认展开的中文部分；修改英文内容时，编辑对应
`.en.md` 文件中默认展开的英文部分。然后同步另一份文件里的语言面板：

```bash
python scripts/sync_doc_languages.py
python scripts/sync_doc_languages.py --check
```

同步命令会生成各文件中的另一种语言；`--check` 只校验、不改写文件，CI 也会执行该检查。
<!-- doc-language:zh-CN:end -->

</details>

<details name="deepcodocx-doc-language" lang="en" open>
<summary>English</summary>

<!-- doc-language:en:start -->
# Development guide

[Home](../README.en.md)

## Environment and checks

```bash
uv sync --extra dev
uv run ruff check src tests scripts
uv run mypy src
uv run pytest -q --cov=dcx --cov-report=term-missing --cov-fail-under=75
node --check src/dcx/web/static/app.js
```

The source package is `src/dcx`; the installed command is `cli-agent`. The CLI,
Web host, and MCP subprocesses all use this package. See
[architecture](architecture.en.md) for module boundaries and
[AGENTS.md](../AGENTS.md) for development rules.

Add a test for every new behavior. Loop tests use
`tests/support/fake_provider.py`. Document tests use small basic / Marker
fixtures and require neither a personal corpus nor an API key.

## Browser acceptance checks

```bash
uv run --with playwright python -m playwright install chromium
uv run --with playwright python -m tests.e2e.web_smoke --output /tmp/deepcodocx-browser
```

This check uses a scripted provider with the real Web server, stdio MCP, and
browser. It covers default libraries, remembered selection, fork/new, search,
images, citations, session isolation, and narrow layouts.

Screenshots in the output directory support visual inspection. Add
`--library /path/to/paper-bundles` to check a real local corpus containing
MEMORYVLA Marker bundles. The corpus is accessed read-only.

## Live-provider tests

```bash
export CLI_AGENT_E2E_PROVIDER=deepseek
export DEEPSEEK_API_KEY="your-api-key"
uv run pytest tests/e2e -q
```

These tests make real provider requests and are skipped when no provider is
configured. Set `CLI_AGENT_E2E_MODEL` to choose the model. The manual CI
`workflow_dispatch` job uses the corresponding repository secret.

## Contribution guidelines

- Keep one agent loop and construct concrete dependencies in assembly.
- Declare each new tool's actual schema and permission category. Only explicitly
  safe tools may run concurrently.
- Keep vendor wire formats inside provider adapters; internal messages remain
  neutral.
- Preserve public JSONL / SSE fields and completion semantics. Keep stdout data
  separate from diagnostics.
- Treat document contents as untrusted data. External tools cannot grant their
  own permissions.
- Cover resume, cancellation, failures, and cleanup while maintaining
  compatibility with existing session records.

Public documentation covers product usage, configuration, architecture, and
contribution. Personal plans, learning notes, and review records belong in
Git-ignored local directories and are excluded from release commits.

## Bilingual documents

GitHub documents use `<details>` elements with a shared name for exclusive
language panels. Clicking a language heading expands its content and collapses
the other language on the same page, without navigation or JavaScript.

Edit Chinese in the default-open Chinese panel of the `.md` file, and English in
the default-open English panel of its `.en.md` counterpart. Then synchronize the
other panel in each file:

```bash
python scripts/sync_doc_languages.py
python scripts/sync_doc_languages.py --check
```

The synchronization command generates the alternative-language panels.
`--check` validates them without writing files; CI runs this check as well.
<!-- doc-language:en:end -->

</details>
