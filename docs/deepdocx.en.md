<!-- Generated language panels. Edit this file's default-open language, then run python scripts/sync_doc_languages.py. -->

<details name="deepcodocx-doc-language" lang="zh-CN">
<summary>简体中文</summary>

<!-- doc-language:zh-CN:start -->
# DeepDocx 文档工作区

[首页](../README.md)

## 启动

在 DeepCodocx 中使用现有 provider 配置：

```bash
uv sync --extra dev
uv run cli-agent web
```

点击顶部 **DeepDocx**，连接文档目录，选择「全新开始」或「Fork 当前对话并继续」。
目录是 Python 服务所在机器的目录，可以填 `deep-docs-unified/data`，也可以直接填
`data/paper-bundles`。界面可进一步选择文档，未勾选时使用整个库。

也可以直接指定已有文档库启动：

```bash
uv run cli-agent web --library /path/to/paper-bundles
```

这条命令直接创建 DeepDocx 会话；若希望选择是否继承普通对话，先启动普通 Web
界面，再通过按钮进入。服务直接消费已转换的 bundle，接入时读取现有文本与图像。

CLI / headless：

```bash
cli-agent run --library /path/to/paper-bundles "结合原文和图示解释 MEMORYVLA 的记忆机制"
cli-agent --mode deepdocx -c
cli-agent run --resume SESSION_ID "继续解释下一部分"
cli-agent run --resume NORMAL_SESSION --fork --library /path/to/paper-bundles "基于已有讨论继续"
```

同一工作目录下，普通 `-c` 只继续最近的普通会话，`--mode deepdocx -c` 继续文档会话。
显式 session id 恢复其已有模式。已有普通模式的 Bash 审批规则保持适用。

## 文档库配置

可在 `~/.config/cli-agent/config.toml` 预设目录：

```toml
[deepdocx]
libraries = ["/path/to/deep-docs-unified/data"]
# 可选：首次进入时的默认库，不必同时添加到 libraries。
default_library = "/path/to/deep-docs-unified/data"
```

通过页面连接的目录持久保存在 `$XDG_DATA_HOME/cli-agent/libraries.json`。
成功进入 DeepDocx 时，同时保存最后选择的文档库；关闭页面、重启服务或创建新会话后，
会自动选中该库，无需重复连接或选择。新增其他目录不会清除已有选择。
优先级为：当前文档会话的库 → 上次成功选择 → `default_library` → 已配置或已登记的库。
没有保存或配置文档库时，会检查当前工作目录及附近上级目录中的
`deep-docs-unified/data`，找到有效 bundle 后直接填入选择器。因此，在相邻目录已存在有效库时，使用
`cli-agent web` 即可选择该语料；`cli-agent web --mode deepdocx` 可直接使用默认库启动。
确实没有可用库时，界面会说明原因并展开目录输入，在连接成功前禁用进入按钮。
会话的模式、库绑定、文档范围和 fork 边界写入追加式 session 记录；普通旧会话默认
`normal`。库的选择不改变 cwd，不加载库中的 AGENTS.md，也不把文档任务规则写入
用户的 Remember 记忆。

源 bundle 和已有 SQLite 索引只读访问。数据目录没有可用索引时退回词法扫描，不在
交互请求中重建源索引。目录中尚未完成的产物和未知 schema 会被略过。
会话内使用已发现的目录快照；转换过程中新增的产物可通过新建文档会话载入。

## 固定工具与 MCP

两种模式均提供固定工具：

```json
{"name":"DiscoverTools","input":{"query":"document paper search read view guide","limit":5}}
```

```json
{"name":"CallExternalTool","input":{"tool":"documents.doc_search","arguments":{"query":"memory","top_k":5}}}
```

上面是调用形状示例，不是 provider wire format。发现结果包含具体工具的描述和
`inputSchema`，模型通过第二个入口调用。发现不会改变 provider 的工具表或 system。
执行前以具体工具 schema 校验参数、以命名空间工具名执行权限检查；未声明只读的
外部工具仍需审批。通用客户端支持 stdio 初始化、工具列表分页、工具调用、服务端
通知、文本/图片结果、超时与取消清理。服务端日志只在 stderr。

增加自己的 stdio MCP 服务：

```toml
[mcp_servers.notes]
command = "python"
args = ["/path/to/notes_server.py"]
read_tools = ["search", "read"]
timeout = 30

[mcp_servers.notes.env]
NOTES_PATH = "/path/to/notes"
```

`read_tools` 是本地配置声明，不从服务端 readOnlyHint 自动授予权限。不要把可修改
环境的工具列为只读。服务名 `documents` 由当前文档库使用。

## 文档 MCP 工具

| 工具 | 用途 | 主要参数 |
|---|---|---|
| documents.doc_library | 列举/按标题定位论文 | query，可省略 |
| documents.doc_search | 按关键词定位文本、图表 | query、doc、kind、top_k≤20 |
| documents.doc_read | 读原文、邻接单元或整页 | locator、neighbors≤20、max_chars≤16000 |
| documents.doc_view | 返回实际页面/图表图片与来源 | locator |
| documents.doc_guide | 按需加载详细操作指南 | 无 |

普通 Bash、Read、Grep 仍可组合使用。指南解释两种 bundle 的文件结构，agent 可以直接
读已有图片，不用再次解析 PDF。工具不规划任务，不使用另一个回答模型。

文档服务可以单独运行，便于其他 MCP 客户端使用：

```bash
python -m dcx.deepdocx.server --root /path/to/paper-bundles
```

## 来源与异构产物

示例定位符：

```text
doc:paper_6d40ee70d189@v1/basic/unit:c_0001_0000
doc:paper_6d40ee70d189@v1/marker-json-v1/unit:u_000037
doc:paper_6d40ee70d189@v1/marker-json-v1/page:3
```

定位符显式包含源版本和 rendition；同一源的 basic/Marker 不会按 unit id 互相替换。
工具结果带 library、doc、version、rendition、page、bbox（存在时）和 locator。
回答使用 `[来源](doc:...)` 可以打开右侧预览；工具卡中也有「打开来源」按钮。
预览同时显示邻接文本和已有页图或图表裁剪。模型要实际看图时应使用 doc_view 或 Read。

basic 单元是行级文本，应读邻接行/整页；Marker 单元保留 paragraph、figure、table、
equation 等类型。FTS5 用于已有 basic 索引，Marker 使用词法定位。原始图注可能缺失，
图像与数学公式仍需模型读取原图验证。没有图表 crop 时使用所在页面。

这些定位符属于本项目的文档 MCP 接口。接入其他服务时，应采用其返回的实际 schema。

## 会话与界面行为

- DeepDocx 与普通模式分别展示会话，返回 Agent 会打开最近普通会话。
- fork 保留源会话，并复制历史及会话资源；新会话的代码回滚不能跨过 fork 边界。
- fork 共享原来的 coding 工作目录，不创建 worktree。继承历史在界面中只读。
- 新建、恢复、压缩和清空对话后，文档模式及库配置继续有效。
- 模式引导保存在消息的独立 host_context 字段中，provider 请求将其转换为带来源
  标记的背景内容；聊天正文仍显示用户原始输入。旧请求前缀不会因下轮输入而移除指南。
- 切换库/文档范围通过创建新的 DeepDocx 会话完成，可选择 fork 继承当前讨论。
- 来源面板按需打开，窄屏使用覆盖面板；宽屏为三栏。主题和尺寸偏好保存在浏览器。

## 当前能力边界

向量搜索、图像相似检索、生成 caption、语义块索引、PDF 上传入库、远程 MCP 和多用户
未在本版本实现。上传图片可使用现有视觉模型先提取文字，再调用关键词定位；无文字
的图片需要模型利用可用线索查找，或等待后续向量工具。系统不会声称已经做了向量检索。

模型必须实际支持图片输入才能分析图像；只读元数据或在浏览器看到图片不代表模型已
看到图片。联网能力也取决于是否配置了真实可调用的外部工具。


开发环境与浏览器验收见[开发指南](development.md)。
<!-- doc-language:zh-CN:end -->

</details>

<details name="deepcodocx-doc-language" lang="en" open>
<summary>English</summary>

<!-- doc-language:en:start -->
# DeepDocx document workspace

[Home](../README.en.md)

## Start a document session

Use your existing provider configuration in DeepCodocx:

```bash
uv sync --extra dev
uv run cli-agent web
```

Click **DeepDocx**, connect a document directory, and choose a fresh conversation
or a fork of the current discussion. The directory is on the machine running the
Python server. You can select `deep-docs-unified/data` or its
`data/paper-bundles` directory directly. Select individual documents to narrow the
scope; leaving the selection empty uses the whole library.

You can also start with an existing library directly:

```bash
uv run cli-agent web --library /path/to/paper-bundles
```

This creates a DeepDocx session immediately. To choose whether to inherit a
general conversation, start the general Web interface first and enter through
the button. The document service consumes preprocessed bundles and reads their
existing text and images.

CLI / headless examples:

```bash
cli-agent run --library /path/to/paper-bundles "Explain MEMORYVLA's memory mechanism using the original text and figures"
cli-agent --mode deepdocx -c
cli-agent run --resume SESSION_ID "Continue with the next section"
cli-agent run --resume NORMAL_SESSION --fork --library /path/to/paper-bundles "Continue from our earlier discussion"
```

Within a workspace, plain `-c` resumes the latest general session;
`--mode deepdocx -c` resumes the latest document session. An explicit session ID
restores its saved mode. Existing Bash approval rules continue to apply.

## Library configuration

You can configure directories in `~/.config/cli-agent/config.toml`:

```toml
[deepdocx]
libraries = ["/path/to/deep-docs-unified/data"]
# Optional initial default; it does not also need to appear in libraries.
default_library = "/path/to/deep-docs-unified/data"
```

Directories connected through the UI are stored in
`$XDG_DATA_HOME/cli-agent/libraries.json`. Successfully entering DeepDocx also
saves the selected library. It is selected again after closing the page,
restarting the server, or creating another session. Registering another
directory preserves the previous selection.

Selection priority is: the current document session's binding, the last
successful selection, `default_library`, then an available configured or saved
library. When none is saved or configured, the app checks for
`deep-docs-unified/data` near the current workspace. A valid bundle library is
added to the selector automatically. In that layout, `cli-agent web` can offer
the library without manual registration, and `cli-agent web --mode deepdocx`
can use it directly. If no valid library exists, the dialog explains how to
connect one and disables entry until one is available.

Session mode, library bindings, document scope, and fork boundaries are stored in
append-only session records. Older sessions default to `normal`. Selecting a
library does not change the coding working directory, load the library's
AGENTS.md, or save mode instructions through Remember.

Source bundles and existing SQLite indexes are accessed read-only. An absent
index falls back to lexical scanning; interactive requests do not rebuild the
source index. Incomplete bundles and unsupported schemas are skipped. A session
uses a snapshot of the discovered directory; create a new document session to
pick up newly converted bundles.

## Fixed tool entries and MCP

Both modes expose the same fixed entries:

```json
{"name":"DiscoverTools","input":{"query":"document paper search read view guide","limit":5}}
```

```json
{"name":"CallExternalTool","input":{"tool":"documents.doc_search","arguments":{"query":"memory","top_k":5}}}
```

These examples illustrate the call shape, not a provider's wire format.
Discovery returns tool descriptions and `inputSchema`; the model invokes a
discovered tool through the second entry. Discovery does not change the
provider-facing tool list or system prompt.

Execution validates arguments against the actual tool schema and checks
permissions under its namespaced tool name. External tools not declared
read-only require approval. The generic client handles stdio initialization,
paginated tool lists, calls, server notifications, text/image results, timeouts,
and cancellation cleanup. Server diagnostics go to stderr.

To add your own stdio MCP service:

```toml
[mcp_servers.notes]
command = "python"
args = ["/path/to/notes_server.py"]
read_tools = ["search", "read"]
timeout = 30

[mcp_servers.notes.env]
NOTES_PATH = "/path/to/notes"
```

`read_tools` is a local declaration. A server's `readOnlyHint` does not grant
permissions automatically. Only list tools whose operations are read-only.
The name `documents` is reserved for the selected document library.

## Document MCP tools

| Tool | Purpose | Main arguments |
|---|---|---|
| `documents.doc_library` | List papers or resolve them by title | Optional `query` |
| `documents.doc_search` | Locate text, figures, and tables with keywords | `query`, `doc`, `kind`, `top_k` ≤ 20 |
| `documents.doc_read` | Read a source unit, its neighbors, or a page | `locator`, `neighbors` ≤ 20, `max_chars` ≤ 16000 |
| `documents.doc_view` | Return an actual page or figure image with its source | `locator` |
| `documents.doc_guide` | Load detailed workflow guidance on demand | None |

Native Bash, Read, and Grep can be combined with these tools. The guide explains
both bundle layouts so the agent can inspect existing images without parsing
the PDF again. The document tools return evidence; planning and answering
remain with the main agent.

The document service can also run independently for other MCP clients:

```bash
python -m dcx.deepdocx.server --root /path/to/paper-bundles
```

## Sources and heterogeneous renditions

Example locators:

```text
doc:paper_6d40ee70d189@v1/basic/unit:c_0001_0000
doc:paper_6d40ee70d189@v1/marker-json-v1/unit:u_000037
doc:paper_6d40ee70d189@v1/marker-json-v1/page:3
```

Locators explicitly include the source version and rendition. Units from basic
and Marker are not substituted for one another based on unit ID. Results
include the library, document, version, rendition, page, bbox when available, and
locator.

Answers can use `[source](doc:...)` links to open the source preview. Tool cards
also provide source buttons. The preview shows nearby text and the existing
page image or figure crop. To give the model actual image input, use `doc_view`
or Read.

Basic units are lines; read neighboring units or the whole page for context.
Marker retains paragraph, figure, table, equation, and other unit types. The
existing basic index uses FTS5; Marker uses lexical matching. Original captions
may be missing, and figures or equations may need visual inspection to verify
details. If no crop is available, the service uses the containing page.

These locators belong to this project's document MCP interface. When connecting
another service, use its actual returned schema.

## Session and interface behavior

- DeepDocx and general sessions have separate history lists. Returning to Agent
  opens the latest general session.
- A fork preserves the source session and copies its history and resources.
  Code rewind cannot cross the new session's fork boundary.
- Forks share the coding working directory; no Git worktree is created.
  Inherited history is read-only in the interface.
- Library bindings and document mode remain available after new-session,
  resume, compact, and clear operations.
- Mode guidance is saved separately as `host_context` on messages and rendered
  into provider input with a host-source marker. The chat displays the user's
  original input. A later turn does not remove the guidance from earlier
  request prefixes.
- Switching libraries or scope creates a new DeepDocx session, with an option
  to fork the current discussion.
- Source previews open on demand. Narrow screens use an overlay; wide screens
  use three columns. Theme and size preferences stay in the browser.

## Current limitations

Vector search, image similarity retrieval, generated captions, semantic block
indexes, PDF upload/ingestion, remote MCP transports, and multi-user operation
are not implemented in this version. For an uploaded image, a vision-capable
model can extract visible terms and try keyword lookup. Images without text
need other available clues or future vector tools. The agent should not claim
to have performed vector search.

Image analysis requires actual image support from the model and endpoint.
Reading metadata or showing an image in the browser does not mean the model
received it. Network search likewise requires a real available external tool.

See the [development guide](development.en.md) for environment setup and browser
acceptance checks.
<!-- doc-language:en:end -->

</details>
