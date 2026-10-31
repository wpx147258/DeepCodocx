# DeepDocx 文档工作区

**简体中文** | [English](deepdocx.en.md) · [首页](../README.md)

## 启动

在 DeepCodocx 中使用现有 provider 配置：

```bash
uv sync
uv run dcx web
```

点击顶部 **DeepDocx**，连接文档目录，选择「全新开始」或「Fork 当前对话并继续」。
目录是 Python 服务所在机器的目录，可以填 `deep-docs-unified/data`，也可以直接填
`data/paper-bundles`。界面可进一步选择文档，未勾选时使用整个库。

也可以直接指定已有文档库启动：

```bash
uv run dcx web --library /path/to/paper-bundles
```

这条命令直接创建 DeepDocx 会话；若希望选择是否继承普通对话，先启动普通 Web
界面，再通过按钮进入。服务直接消费已转换的 bundle，接入时读取现有文本与图像。

CLI / headless：

```bash
dcx run --library /path/to/paper-bundles "结合原文和图示解释 MEMORYVLA 的记忆机制"
dcx --mode deepdocx -c
dcx run --resume SESSION_ID "继续解释下一部分"
dcx run --resume NORMAL_SESSION --fork --library /path/to/paper-bundles "基于已有讨论继续"
```

同一工作目录下，普通 `-c` 只继续最近的普通会话，`--mode deepdocx -c` 继续文档会话。
显式 session id 恢复其已有模式。已有普通模式的 Bash 审批规则保持适用。

## 文档库配置

可在 `~/.config/dcx/config.toml` 预设目录：

```toml
[deepdocx]
libraries = ["/path/to/deep-docs-unified/data"]
# 可选：首次进入时的默认库，不必同时添加到 libraries。
default_library = "/path/to/deep-docs-unified/data"
```

通过页面连接的目录持久保存在 `$XDG_DATA_HOME/dcx/libraries.json`。
成功进入 DeepDocx 时，同时保存最后选择的文档库；关闭页面、重启服务或创建新会话后，
会自动选中该库，无需重复连接或选择。新增其他目录不会清除已有选择。
优先级为：当前文档会话的库 → 上次成功选择 → `default_library` → 已配置或已登记的库。
没有保存或配置文档库时，会检查当前工作目录及附近上级目录中的
`deep-docs-unified/data`，找到有效 bundle 后直接填入选择器。因此，在相邻目录已存在有效库时，使用
`dcx web` 即可选择该语料；`dcx web --mode deepdocx` 可直接使用默认库启动。
确实没有可用库时，界面会说明原因并展开目录输入，在连接成功前禁用进入按钮。
会话的模式、库绑定、文档范围和 fork 边界写入追加式 session 记录；普通旧会话默认
`normal`。库的选择不改变 cwd，不加载库中的 AGENTS.md，也不把文档任务规则写入
用户的 Remember 记忆。

文档读取工具只读访问源 bundle 与已有全文目录。没有可用全文目录时退回词法扫描，
不在交互请求中重建它。目录中尚未完成的产物和未知 schema 会被略过。
通过 Web／CLI 入库后，库版本通知会让 Web 与已运行的 MCP 在下一次读取时刷新缓存。
会话已限定文档范围时，新文档不会自动加入该范围；切换范围需创建新会话或 fork。
外部直接复制的 bundle 可通过新建文档会话重新发现，语义索引需手动同步。

## 多模态语义定位

已有文档库可以直接构建索引，无需重新解析 PDF。索引管理按四类展示能力：

| 索引类别 | 内容 | 使用场景 |
|---|---|---|
| 正文块 | Marker 段落／列表，或按版面合并的 Basic 文本行 | 解释概念、按语义定位段落 |
| 图像自身 | 图表裁图；只有 Basic 时使用页面图 | 以图搜图、按文字寻找图示 |
| 图注与生成描述 | 原图注和视觉模型描述分别保存 | 按图注或图像内容描述定位 |
| 原生图文联合 | 图像＋原图注、图像＋生成描述 | 同时利用图像与文字线索 |

在启动 Web 的终端配置 `GITEE_API_KEY` 与 `DASHSCOPE_API_KEY` 后，进入 DeepDocx，
点击文档库旁的 **索引管理**。按标题搜索并勾选论文，选择是否生成描述及描述模型，
再点击 **同步索引**。也可显式选择整个文档库；单篇同步不影响其它论文已有索引。
首次手动同步为当前库启用后续入库自动同步；仅设置密钥不会自动构建全部语料。
同步和生成描述属于独立后台任务，进度不写入对话。可以关闭面板后继续提问。

在面板的 **试用语义检索** 中输入文字、上传图片，或同时提供两者；点击结果打开
原文／原图。该预览用于直接试查，不会创建 Agent 回合。对话中的 Agent 则按需发现
语义工具，继续自行组合关键词、Bash、Read 和图片读取。

默认独立编码与原生联合编码均使用百炼 `qwen3-vl-embedding`，均为 2560 维；
Gitee 可显式配置为独立编码备用，图片描述仍使用 Gitee。查询通道分别编码后按排名融合。
没有可用或当前索引时，语义工具会
返回明确提示，Agent 可继续关键词定位；不会在一次提问中自动启动建库。
完整配置、同步规则及 caption 来源说明见[多模态索引](multimodal-index.md)。

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
| documents.doc_index_status | 查看当前库／会话范围内的语义索引覆盖与状态 | 无 |
| documents.doc_semantic_search | 按文字、图片或图文定位证据 | query、image_ref、target=auto/text/image、doc、top_k（默认 5，最多 20） |

普通 Bash、Read、Grep 仍可组合使用。指南解释两种 bundle 的文件结构，agent 可以直接
读已有图片，不用再次解析 PDF。工具不规划任务，不使用另一个回答模型。

语义工具通过宿主配置接入，具体可用项以 `DiscoverTools` 返回为准。例如先发现
`document semantic search index status`，再调用：

```json
{"name":"CallExternalTool","input":{"tool":"documents.doc_semantic_search","arguments":{"query":"记忆模块如何连接动作预测","target":"text","top_k":5}}}
```

`query` 与图片至少提供一种。聊天上传图片会获得 `attachment:...` 稳定引用，宿主把它
加入消息上下文，模型可直接传为 `image_ref`；库内图像也可用完整 `doc:...` 定位符。
`target=auto` 对纯文字查询定位正文／图注，对图片查询定位图像／原生图文；`text`
只查正文块，`image` 查图像相关表征。工具不替模型规划后续任务。

默认返回最多 5 条紧凑来源卡片及 `coverage`，包括实际可检索的论文名称，避免把未建
索引的论文误判为无相关内容。结果不附图片像素或整段正文，需要用 `doc_read`／
`doc_view` 核对。生成描述通过 `snippet_source` 和生成来源标记区分。
旧参数 `image_locator`／`image_path` 与高级 `representations` 仍兼容；三种图片输入
互斥，显式 `target=text/image` 不与高级表征过滤同时使用。图片路径限于所选文档库、
工作区或当前会话，不接受远程 URL。

附件文件保存在当前会话的 `attachments/`，恢复会话与 fork 后保持引用可用；原始消息
与历史记录不被改写。侧栏“语义索引 x/y”可查看覆盖，对话工具卡直接展示命中片段和
来源按钮，原始 JSON 可展开查看。

文档服务可以单独运行，便于其他 MCP 客户端使用：

```bash
python -m dcx.deepdocx.server --root /path/to/paper-bundles
```

上面的独立命令默认提供基础文档工具；dcx 宿主会另外传入无密钥的语义服务设置。
其它客户端接入语义能力的方式见[索引指南](multimodal-index.md)。

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

语义文本块不会改变这些原始单元：每块保留全部来源 locator、标题、章节及 bbox。
图注／联合检索返回的来源仍指向原图与原文，caption 的生成模型和输入来源另行记录。

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

可选的语义文本块、图像、caption 和原生图文联合索引已接入，见[多模态索引](multimodal-index.md)。
Agent 可以按需发现 doc_index_status / doc_semantic_search；没有当前索引时继续使用关键词和原生文件工具。
生成描述明确标记来源，不能作为论文原文引用。PDF 入库和索引写入由独立系统任务完成，
不暴露为模型写工具。远程 MCP 和多用户仍在后续范围。

模型必须实际支持图片输入才能分析图像；只读元数据或在浏览器看到图片不代表模型已
看到图片。联网能力也取决于是否配置了真实可调用的外部工具。


开发环境与浏览器验收见[开发指南](development.md)。
