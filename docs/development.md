# 开发指南

**简体中文** | [English](development.en.md) · [首页](../README.md)

## 环境与检查

```bash
uv sync --extra dev
uv run ruff check src tests
uv run mypy src
uv run pytest -q --cov=dcx --cov-report=term-missing --cov-fail-under=75
node --check src/dcx/web/static/app.js
```

源码包为 `src/dcx`，安装后的命令是 `dcx`。CLI、Web 和 MCP 子进程均使用此包。
模块边界见[架构说明](architecture.md)，开发约定见 [AGENTS.md](../AGENTS.md)。

每个新增行为应有测试。Loop 测试使用 `tests/support/fake_provider.py`；文档测试通过
微型 basic / Marker fixtures 运行，不依赖个人语料或 API key。

## 浏览器验收

```bash
uv run --with playwright python -m playwright install chromium
uv run --with playwright python -m tests.e2e.web_smoke --output /tmp/deepcodocx-browser
uv run --with playwright python -m tests.e2e.ingest_smoke --output /tmp/deepcodocx-ingest
```

该验收使用脚本 provider，运行真实 Web 服务、stdio MCP 和浏览器，检查旧版浏览器设置迁移、文档库默认值、
选择记忆、fork/new、搜索、图片、引用、会话隔离及窄屏布局。
入库验收需要 Poppler，使用微型 PDF 运行真实上传、独立 worker 与索引，检查小窗口侧栏、
移动端弹窗、选项联动和取消恢复，并断言没有模型调用或对话写入。见[文档入库](deepdocx-ingest.md)。
输出目录中的截图用于界面核对。附加 `--library /path/to/paper-bundles` 可检查包含
MEMORYVLA Marker 产物的真实本地语料，目录只读访问。

## 真实 provider 测试

```bash
export DCX_E2E_PROVIDER=deepseek
export DEEPSEEK_API_KEY="your-api-key"
uv run pytest tests/e2e -q
```

这组测试会请求真实服务；未设置 provider 时默认跳过。模型可通过
`DCX_E2E_MODEL` 指定。CI 的手动 `workflow_dispatch` 任务使用对应仓库 secret。

## 贡献方向

- 维持唯一 Agent loop，具体对象集中在 assembly 装配。
- 新工具声明真实 schema 与权限分类；只有明确安全的工具才允许并发。
- 新 provider 只在 adapter 中处理 wire format，内部消息保持中立。
- 维护 JSONL / SSE 的公开字段与完成语义，stdout 与诊断分离。
- 文档内容按不可信数据处理；外部工具不能自行授予权限。
- 检查恢复、取消、异常与资源释放路径，同时保持旧会话记录兼容。

仓库公开文档维护产品使用、配置、架构和贡献说明。个人计划、学习笔记与审查过程保存在
Git 忽略的本地目录，不加入发布提交。
