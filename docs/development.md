# 开发指南

**简体中文** | [English](development.en.md) · [首页](../README.md)

## 环境与检查

```bash
uv sync --extra dev
uv run ruff check src tests
uv run mypy src
uv run pytest -q --cov=dcx --cov-report=term-missing --cov-fail-under=75
node --check src/dcx/web/static/app.js
node --check src/dcx/web/static/diagrams.js
node --check src/dcx/web/static/indexes.js
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
uv run --with playwright python -m tests.e2e.semantic_smoke --output /tmp/deepcodocx-index
uv run --with playwright python -m tests.e2e.mermaid_smoke --output /tmp/deepcodocx-mermaid
uv run --with playwright python -m tests.e2e.document_search_smoke --output /tmp/deepcodocx-document-chat
```

该验收使用脚本 provider，运行真实 Web 服务、stdio MCP 和浏览器，检查旧版浏览器设置迁移、文档库默认值、
选择记忆、fork/new、搜索、图片、引用、会话隔离及窄屏布局。
入库验收需要 Poppler，使用微型 PDF 运行真实上传、独立 worker 与索引，检查小窗口侧栏、
移动端弹窗、选项联动和取消恢复，并断言没有模型调用或对话写入。见[文档入库](deepdocx-ingest.md)。
语义验收使用本地推理 fixture、微型 bundle 与真实索引 worker，覆盖四类计数、描述模型
选择、缓存增量同步、文字试查、原生图文请求、来源回读、深色与移动端布局，不调用付费推理服务。
Mermaid 验收使用真实渲染器检查图表／源码切换、复制、缩放与拖动、主题切换、语法错误
回退、流式回复和历史恢复；不需要网络模型服务。
文档对话验收使用真实 runtime／stdio MCP 和本地推理 fixture，检查附件引用、目标路由、
紧凑来源卡片、覆盖徽标、回读原图、主题与历史恢复。
输出目录中的截图用于界面核对。`web_smoke` 附加 `--library /path/to/paper-bundles` 可检查包含
MEMORYVLA Marker 产物的真实本地语料，目录只读访问。

## 多模态索引的开发边界

`deepdocx/semantic/` 内部按职责组织：`settings.py` 管配置与 profile，`samples.py`
构建有界语义块及图像样本，`client.py` 适配推理接口，`store.py` 管 SQLite／sqlite-vec
和缓存，`engine.py` 负责同步与检索，`service.py`／`worker.py` 负责持久任务。
`tools.py` 只披露读侧工具；管理入口位于 `cli/indexes.py`、`web/indexes.py` 和
`web/static/indexes.js`。整体关系见[架构说明](architecture.md)。

```bash
uv run pytest tests/unit/test_semantic.py tests/unit/test_semantic_service.py -q
```

离线测试使用 `tests/support/inference_fixture.py` 提供的协议 fixture，覆盖表征、
向量空间隔离、增量缓存、过期／删除来源、会话范围、部分失败、任务重启检测和取消。
新增编码器时保持每个空间独立的 query encoder；修改预处理、块构建或 caption prompt
时同步更新相应版本，使旧代结果可识别。检索必须保留原始 locator 与生成来源，
禁止把模型描述写回原文或把平均向量标为原生联合编码。

真实 Gitee／百炼验证与对话 provider 测试是两组检查；后者不验证 embedding 服务。
真实索引验证应对小型独立库运行 `docs index sync/search`，需要对应密钥且可能计费。
可选同型号服务的协议测试也不等于实际运行过本地 8B 模型，部署见
[原生联合服务](../services/qwen-native/README.md)。

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
- 文档入库和索引写侧维持独立系统任务；不向主 Agent 增加建库／caption 写工具。
- 保持 bundle 源证据与 XDG 派生数据分离，按内容指纹、配置代次和范围过滤查询。
- 检查恢复、取消、异常与资源释放路径，同时保持旧会话记录兼容。

仓库公开文档维护产品使用、配置、架构和贡献说明。个人计划、学习笔记、审查过程、
项目书、演讲材料与测试报告仅在本地保留，通过 Git 忽略规则排除，不加入发布提交。
