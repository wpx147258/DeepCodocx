# DeepDocx 文档入库

DeepDocx 的读侧是只读的 Agent 工具；文档入库是 **系统能力**，不通过 AgentRuntime，
也不暴露成 `doc_build` 之类的模型工具。

## 入口

### Web

进入 DeepDocx 后，左侧 **添加文档**：

1. 选择一个或多个 PDF；
2. 选择 `Basic`、`Marker` 或 `Both`；
3. 点击「开始生成」；
4. 在任务卡中查看进度；
5. 完成后刷新库，建议新建文档会话。

浏览器只上传文件；文件保存在服务端当前文档库的 `sources/` 下。不会把任意浏览器路径
当成服务端路径。

### CLI

```bash
dcx docs ingest --library /path/to/data --mode basic paper.pdf
dcx docs ingest --library /path/to/data --mode both paper1.pdf paper2.pdf
dcx docs jobs
dcx docs cancel <job-id>
dcx docs list --library /path/to/data
```

`--force` 才会替换已存在的 rendition；默认跳过。

![添加文档对话框](web/deepdocx-ingest.png)

## 生成方式

| 方式 | 产物 | 特点 |
|---|---|---|
| Basic | `v1/basic`，`dda.bundle/1` | Poppler；行级文本、页码、bbox、72 DPI 页面图；快 |
| Marker | `v1/marker`，`dda.bundle/3` | Marker；段落、figure、table、caption；慢，需要 Marker 环境 |
| Both | 先 Basic，再 Marker | Basic 立即可用，Marker 后续增强 |

同一篇文档的两个 rendition 共用 `doc_id/version`，locator 显式带 rendition：

```text
doc:paper_xxx@v1/basic/unit:c_...
doc:paper_xxx@v1/marker-json-v1/unit:u_...
```

## 落盘

```text
<library>/
  sources/<doc_id>.pdf         上传的原始 PDF
  paper-bundles/
    <doc_id>/v1/basic/...
    <doc_id>/v1/marker/...
    catalog.sqlite             Basic 的 FTS 索引
```

上传临时文件位于 `$XDG_DATA_HOME/dcx/ingest/jobs/<job>/uploads/`；成功或部分成功后清理。
任务记录位于同一 `ingest/` 目录。

发布顺序：

```text
staging
→ 校验 manifest / required files
→ 原子 rename 到 paper-bundles
→ 复制 source 到 sources/
→ Basic 后原子重建 catalog.sqlite
```

失败或取消不会留下半个 bundle。

## 配置

```toml
[deepdocx.ingest]
basic_dpi = 72
max_upload_mb = 200
max_files = 50
marker_command = "/path/to/miniconda3/bin/marker_single"
marker_timeout = 1800
```

- `pdftotext` / `pdftoppm` 必须在 worker 的 PATH 中；
- `marker_command` 必须指向 Marker 可执行文件；Web 会先检测，未就绪时禁用 Marker；
- 不在请求中自动安装 Marker，也不把 Marker import 到 Web 进程；
- Marker 一次只运行一个任务。

## 当前边界

- 首版只接受 PDF；其他格式后续扩展；
- 没有 OCR：扫描版 PDF 的 Basic 会得到 `NO_TEXT` 质量标记；
- 没有向量索引；Basic 是词法 FTS，Marker 是词法扫描；
- 上传的是服务端本机文件流，不是远程对象存储；
- Agent 不能触发入库，也不会在对话中等待长时间构建。
