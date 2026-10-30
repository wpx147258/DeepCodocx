# 多模态索引与语义定位

**简体中文** | [English](multimodal-index.en.md) · [首页](../README.md)

DeepDocx 支持正文语义块、图像、原图注／生成描述、原生图文联合四类向量索引。
它们用于定位文档证据：检索返回片段、页码和原始 locator，随后由同一个 Agent 自主
阅读原文、看图和回答。关键词检索、Bash、Read 与现有 Agent loop 继续可用。

## 模型与向量空间

| 能力 | 服务与模型 | 数据表示 |
|---|---|---|
| 正文块、图像、原图注、生成描述 | Gitee AI `Qwen3-VL-Embedding-8B` | Gitee 空间，默认 1024 维 |
| 图像＋原图注、图像＋生成描述 | 阿里云百炼 `qwen3-vl-embedding`，`enable_fusion=true` | 独立联合空间，默认 1024 维 |
| 生成图片描述 | Gitee AI `qwen3.8-flash` 或 `DeepSeek-V4.1-Flash-1017` | 文字及生成来源信息；再送入 embedding 服务 |

Gitee 当前的在线网关可接收文本、图片独立样本，但不接受同一样本同时包含两者。
百炼提供原生融合接口，但其模型 ID、维度规格与 Gitee 8B 服务不同，不能声称它们是
同一向量空间。**即使输出维度相同，也不能直接计算跨服务的 cosine 相似度。**

实现中为两种空间分别编码查询、计算相似度，再通过 reciprocal rank fusion（RRF）
合并候选排序。文字＋图片查询也会分别进行 Gitee 文字／图片定位，以及百炼联合定位。
返回值保留每个空间的相似度和命中的表征；最终 `score` 是排序分数，不是跨空间相似度。
没有使用平均图片／caption 向量冒充原生融合。

如果以后部署了真正同型号的原生联合服务，可切换到兼容端点，见本文最后一节。

## 开始使用

在启动 dcx 的终端设置服务密钥环境变量：

```bash
export GITEE_API_KEY="your-gitee-key"
export DASHSCOPE_API_KEY="your-dashscope-key"
uv sync
uv run dcx web --provider deepseek
```

密钥只从服务端环境读取，不进入浏览器、任务 JSON、bundle 或 Agent 提示词。
更新 shell 配置后，重启 Web 进程使其继承环境。默认百炼地址属于北京地域；其它地域
需要配置与 API key 对应的 endpoint。

进入 DeepDocx，点击文档库旁的 **索引管理**：

1. 查看完整／部分同步文档数和四类表征数量。
2. 选择是否生成图片描述，以及使用的描述模型。
3. 点击 **同步索引**。首次同步建立索引，并为当前库记住新文档自动同步偏好。
4. 任务在后台运行，可关闭面板，稍后重新打开；重启服务后仍可查看持久任务记录。
5. 在 **试用语义检索** 中输入文字、上传图片，或同时提供两者。点击来源打开已有证据面板。

![索引管理](web/deepdocx-index-light.png)

[暗色主题](web/deepdocx-index-dark.png) · [移动端](web/deepdocx-index-mobile.png)

**重新生成** 会跳过推理缓存，重新请求当前库全部文档的 embedding 与所选 caption。
普通同步会复用内容不变的结果。外部服务可能计费，首次全库构建及重新生成的请求数量
取决于文本块和图像数量；页面图片按 page 标记，不会冒充提取出的 figure。

## 配置

```toml
[deepdocx.index]
enabled = true
# 默认 false；某个文档库首次手动同步后，会为该库启用自动同步。
auto_sync = false
base_url = "https://ai.gitee.com/v1"
api_key_env = "GITEE_API_KEY"
model = "Qwen3-VL-Embedding-8B"
dimensions = 1024
batch_size = 8
timeout = 60
block_chars = 1800

joint_protocol = "dashscope"
joint_base_url = "https://dashscope.aliyuncs.com/api/v1"
joint_key_env = "DASHSCOPE_API_KEY"
joint_model = "qwen3-vl-embedding"
joint_dimensions = 1024
revision = "qwen3-vl-v1"

[deepdocx.caption]
enabled = true
base_url = "https://ai.gitee.com/v1"
api_key_env = "GITEE_API_KEY"
model = "qwen3.8-flash"
# 也支持 "DeepSeek-V4.1-Flash-1017"，可在索引面板按库选择。
max_tokens = 800
timeout = 90
revision = "scientific-description-v1"
```

Embedding 固定使用指定的 Gitee 模型；维度为 64–4096。百炼联合服务支持的维度是
256、512、768、1024、1536、2048、2560，两种维度可以不同。caption 模型或 prompt
版本、embedding 模型配置、块构建配置发生变化时，旧代索引会被识别为待同步。
服务状态的“已配置”表示环境变量和配置存在，实际服务或模型失败会显示在任务结果中。

## Bundle 与数据库如何同步

Bundle 是原始证据，向量库是可重建的派生数据。**向量、caption 和任务状态均不写回
bundle**，避免改变 manifest、校验和及历史引用。已有语料可以直接同步，不必重新解析 PDF。

```text
$XDG_DATA_HOME/dcx/semantic/<library-id>/
  index.sqlite                 向量、来源、内容指纹、缓存、偏好和任务状态
  index.sqlite-wal             SQLite 正常运行文件
  jobs/<job-id>/settings.json   任务配置快照（无密钥）
  jobs/<job-id>/worker.log      后台日志
```

库身份沿用规范化根路径的 library ID。移动库后新路径有新的 ID，需要同步新库；它不会
改写原库索引。每条记录关联 doc ID、版本、rendition、unit／asset、源 locator 和内容指纹。

| 事件 | 行为 |
|---|---|
| 首次同步 | 枚举完整 bundle，构建文本块、图像样本和四类向量 |
| 内容未变 | 跳过文档，不请求推理服务 |
| 通过 Web／CLI 新入库 | 已启用自动同步的库会排入独立索引任务；基础转换不等待模型增强 |
| 入库发生在同步期间 | 合并一次后续增量扫描，避免遗漏新文档或启动竞争写入者 |
| 原 bundle 被替换／修改 | 查询核对指纹，立即排除过期向量；下次同步原子替换该文档记录 |
| 外部复制／删除 bundle | 查询排除已消失／过期的来源；手动同步添加新记录并清理删除记录 |
| embedding／caption 配置变化 | 不混用旧代结果；复用仍适用的输入缓存并补齐新表征 |
| 某类服务失败 | 已完成的其它表征仍可用，文档显示部分完成；再次同步重试缺失内容 |
| 取消 | 不再开始后续工作；已提交的完整文档保留，已完成的推理缓存可复用 |

内容哈希会根据文件大小、mtime 和 ctime 缓存。每次查询仍确认当前库和所选文档范围，
不能因为数据库里有旧记录就返回已删除的证据。单文档生成结果在 SQLite 事务中一起
提交；失败不会暴露半条向量。向量缓存按实际输入和配置区分，生成 caption 另行缓存。

首版使用 SQLite + sqlite-vec 的原生 cosine 距离函数做精确向量检索，无需启动额外
数据库服务。它适合当前本地文档库规模；非常大的库需要后续的 ANN 后端和性能评估，
当前没有把这部分宣称为已实现。

## 文本块、图像和 caption

同一源版本优先使用 Marker 的段落／列表等单元，短段可在相同章节和页面内合并，长段
有界拆分。Basic 按页面坐标、栏位和段间距合并文本行；这是可追溯的启发式重建，不等同于
完整的版面理解。新生成的 Basic 会按明显栏间距拆开同一水平线上的双栏文字；旧产物若
已在转换时混合两栏，派生索引无法恢复丢失的词坐标，段落质量优先采用 Marker。
块记录其全部来源 locator、标题、章节和 bbox。

图像优先使用 Marker 的 figure／table 裁图。只有 Basic 的文档使用页面图片，并明确标注
`image_kind=page`。caption 以独立来源保存：

- `caption_original`：论文提取的原始图注。
- `caption_generated`：视觉模型描述，保留模型、版本、生成时间、输入哈希及辅助正文来源。
- `image_caption_original` / `image_caption_generated`：图片与对应文字的原生联合表征。

caption 生成可参考原图注与同页正文，但模型描述不能作为论文原文引用。改换描述模型时
不会覆盖原始图注；检索仍然回到原图与原文。所有编码图片会限制大小，并以保留比例的
缩放图发送给配置好的服务；用户端不需要上传整个 PDF 给 embedding 接口。

## CLI 与只读 MCP

```bash
dcx docs index status --library /path/to/paper-bundles
dcx docs index sync --library /path/to/paper-bundles
dcx docs index sync --library /path/to/paper-bundles --no-captions
dcx docs index sync --library /path/to/paper-bundles --no-wait
dcx docs index cancel --library /path/to/paper-bundles JOB_ID
dcx docs index search --library /path/to/paper-bundles "记忆模块如何连接动作预测"
dcx docs index search --library /path/to/paper-bundles --image figure.png
dcx docs index search --library /path/to/paper-bundles "memory" --image figure.png
```

这些是系统管理命令，无需对话 provider 或 Agent 会话。阻塞同步成功返回 0，部分完成、
失败或取消返回 1；`--force` 重新生成。使用 `--config` 选择独立 TOML 配置。

Agent 通过既有 DiscoverTools / CallExternalTool 按需发现：

- `documents.doc_index_status`：读取覆盖和可用状态。
- `documents.doc_semantic_search`：文字、库内图片 locator、工作区／会话数据中的图片路径查询；
  可以限制文档、表征和 top-k，返回短候选及来源。

模型工具不能构建、删除或重建索引，也不能触发 caption 批处理。工具 schema 按需披露；
完整 system 消息不随索引模式变化。已有 `doc_search` 继续提供关键词定位。

## 可选：同型号联合推理服务

默认使用百炼即可，无需在本机加载 8B 权重。若另有 GPU 推理机器，可按
[部署说明](../services/qwen-native/README.md)提供真正 `Qwen3-VL-Embedding-8B` 的图文端点：

```toml
[deepdocx.index]
joint_protocol = "openai"
joint_base_url = "https://your-inference-host/v1"
joint_key_env = "QWEN_VL_JOINT_API_KEY"
joint_model = "Qwen3-VL-Embedding-8B"
joint_dimensions = 1024
```

即使使用同型号服务，当前实现仍保持空间隔离，避免不同部署、指令或预处理带来的
坐标差异。不会自动下载权重或占用运行 dcx 的机器的 GPU。

接口依据：[Gitee API](https://ai.gitee.com/docs/openapi/v1)、
[百炼多模态向量](https://help.aliyun.com/zh/model-studio/multimodal-embedding-api-reference)、
[Qwen 模型卡](https://huggingface.co/Qwen/Qwen3-VL-Embedding-8B)。
