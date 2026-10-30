# DeepDocx 文档入库

**简体中文** | [English](deepdocx-ingest.en.md) · [首页](../README.md)

将 PDF 添加到已连接的文档库，转换完成后即可定位原文、阅读页面和查看图表。
**上传、转换、发布和索引由系统任务完成**，不调用对话模型，不进入 Agent Runtime，
也不会注册成 `doc_build` 一类的模型工具。

## 在 Web 中添加

1. 进入 DeepDocx 工作区，点击侧栏 **添加文档**，确认目标文档库。
2. 选择或拖入 PDF。文件清单支持移除和继续添加，并显示当前文件数量、大小限制。
3. 选择可用的转换方式；未配置的方式会禁用。环境检查失败时，修复连接后点击 **重新检查**。
4. 点击 **开始生成**，查看各文件、各类产物的结果。

关闭弹窗或点击 **后台运行** 后，任务继续执行；再次打开可查看进度。刷新浏览器后也能
恢复当前文档库尚未结束的任务。**停止任务** 会取消尚未完成的工作；已经发布的完整产物
会保留。上传中刷新页面无法恢复浏览器里的文件流，可以停止该任务，再重新选择文件。

侧栏的会话和文档列表单独滚动，Model、Permissions 和 Context 保持在底部。

![添加文档](web/deepdocx-ingest.png)

[移动端示例](web/deepdocx-ingest-mobile.png) · [暗色主题](web/deepdocx-ingest-dark.png) · [长文档列表与权限区](web/deepdocx-sidebar.png)

## 转换方式与冲突处理

| 方式 | 产物 | 环境与行为 |
|---|---|---|
| Basic | `v1/basic`，`dda.bundle/1` | Poppler；行级文本、页码、bbox、默认 72 DPI 页面预览，较快 |
| Marker | `v1/marker`，`dda.bundle/3` | 独立 Marker 环境及页面渲染组件；段落、figure、table、caption 和公式，较慢 |
| 两种方式 / Both | Basic，再 Marker | 两套依赖均需可用；Basic 发布并更新索引后即可先读，Marker 随后补充 |

系统会同时检查所选方式的所有依赖。没有可用方式、未选文件、格式或大小不符时不能
开始生成；服务端也会独立校验。默认每个文件最大 200 MiB，每批最多 50 个。

文档 ID 兼容已有管线，按规范化文件名生成。同一批中相同内容去重，同名不同内容需先
重命名。已有的所选产物默认跳过，**替换已有产物** / CLI `--force` 用于重新转换同一份
PDF，不替换其它类型的产物。若同名 PDF 内容已经改变，请为新文件改名后添加，避免
Basic 与 Marker 在同一个文档 ID 下指向不同原文。

每种产物分别记录成功或失败。Both 中一个成功、另一个失败会显示 **部分完成**，
索引更新失败也不会报告全部成功；已经发布的文档仍可通过词法扫描读取。

同一文档的 locator 保留版本与产物身份：

```text
doc:paper_xxx@v1/basic/unit:c_...
doc:paper_xxx@v1/marker-json-v1/unit:u_...
```

## 命令行

文档管理命令独立于 Agent，不需要 provider 或 API key：

```bash
# 目标可以是 data/ 或已有的 paper-bundles/；空库需要先创建目录。
mkdir -p /path/to/paper-bundles
dcx docs ingest --library /path/to/paper-bundles --mode basic paper.pdf
dcx docs ingest --library /path/to/paper-bundles --mode both paper1.pdf paper2.pdf
dcx docs ingest --library /path/to/paper-bundles --mode basic --force paper.pdf

dcx docs jobs
dcx docs cancel JOB_ID
dcx docs list --library /path/to/paper-bundles
```

`ingest` 等待任务结束并输出 JSON。全部成功返回退出码 0，部分完成、失败或取消返回 1。
用 `--config /path/to/config.toml` 指定入库设置。CLI 中断会请求取消任务。

## 写入与读取的边界

```mermaid
flowchart LR
    U[Web 上传 / docs CLI] --> S[系统入库服务]
    S --> J[持久任务记录]
    J --> W[独立 worker]
    W --> B[Basic / Marker 转换]
    B --> V[校验与发布]
    V --> L[Bundle 与原始 PDF]
    L --> I[增量 FTS 索引与库版本通知]
    I --> R[Web / 只读文档 MCP]
    R --> A[原有 Agent loop]
```

Agent 只读取产物，不能通过文档 MCP 发起入库。转换进度不写入对话记录，也不触发新的
模型轮次。新产物发布后，Web 与已运行的 MCP 进程在下一次读取时自动刷新缓存。
已限定的会话文档范围保持有效；要使用范围外的新文档，请调整范围并进入新的文档会话。

## 落盘与任务生命周期

```text
<library>/
  sources/<doc_id>.pdf
  paper-bundles/
    <doc_id>/v1/basic/...
    <doc_id>/v1/marker/...
    catalog.sqlite
    .dcx-revision
```

当选择 `paper-bundles/` 根目录时，`sources/` 位于它的同级目录。每个 bundle 也保留
`source/original.pdf`，便于后续回到原文。上传的是文件流，不会把浏览器文件路径当作
服务器路径。

任务和临时上传保存在 `$XDG_DATA_HOME/dcx/ingest/jobs/<job-id>/`，任务状态经过文件锁
和原子写入。服务重启后可继续读取记录，异常退出的 worker 会被标记为失败。
服务端从持久状态判断取消，不会向可能被复用的旧 PID 发送终止信号。

同一任务队列一次运行一个 worker，避免并发 Marker 占满资源；上传与 Agent 对话可继续。
产物在库同一文件系统的 staging 中生成，校验必要文件、页面图片与 Marker 校验和后发布。
替换发布失败时回滚原产物；若回滚本身遇到 I/O 故障，保留 `.dcx-recovery/` 中的原文件
并在任务错误中显示恢复位置。失败或取消不留下可被读取的半成品。

Basic 更新只处理受影响文档的索引行；首次没有索引时才扫描全部 Basic 文档。构建索引
快照后原子替换 `catalog.sqlite`，失败时保留旧索引。新文档尚未进入索引时，读侧可退回
词法扫描。成功、部分完成和取消后的临时上传会清理；失败任务保留输入与 worker 日志。

## 配置

```toml
[deepdocx.ingest]
basic_dpi = 72
max_upload_mb = 200
max_files = 50
marker_command = "/path/to/marker-env/bin/marker_single"
marker_timeout = 1800
```

Basic 需要 `pdftotext` 和 `pdftoppm`；Ubuntu / WSL 可通过系统包管理器安装 `poppler-utils`。
Marker 需要单独准备可执行环境，页面预览与裁图还需要 `pdftoppm` 和项目依赖 Pillow。
`marker_command` 支持带参数的本地命令，路径含空格时需要引号；可执行文件必须可运行。
这里的检查不下载模型，也不能保证外部 Marker 环境的模型权重或 GPU 已经就绪；实际
转换失败会进入任务结果。系统不会在上传请求中自动安装 Marker 或调用对话模型。

首版只接受 PDF，没有向量索引，也没有自动 caption。Basic 不含 OCR；扫描版 PDF
没有可提取文本时保留页面预览，并带有 `NO_TEXT` 质量标记。更多读侧能力见
[DeepDocx 指南](deepdocx.md)。
