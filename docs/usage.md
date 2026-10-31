# CLI 与 Web 使用指南

**简体中文** | [English](usage.en.md) · [首页](../README.md)

## 三个入口

```bash
uv run dcx --provider deepseek                    # 交互式终端
uv run dcx web --provider deepseek                # 本地 Web
uv run dcx run --provider deepseek "介绍这个仓库"   # 一次性执行
```

公共选项可以放在子命令前后。运行 `dcx --help`、`dcx web --help` 或
`dcx run --help` 查看完整参数。Provider 配置见[配置指南](configuration.md)。

## 会话与连续工作

```bash
dcx -c                         # 最近的普通会话
dcx --resume SESSION_ID
dcx --resume SESSION_ID --fork
dcx run -c "继续完成剩下的工作"
dcx --mode deepdocx -c          # 最近的文档会话
```

显式 provider/model 覆盖保存的设置；仅更换 provider 时采用新 provider 的默认模型。
保存的权限模式随会话恢复，显式 `--permission-mode` 优先。

Fork 复制历史、用量和会话资源，继续写入新 session。普通会话与 DeepDocx 会话分别
展示；进入文档模式可以选择 fork 或全新开始。文档操作见[文档指南](deepdocx.md)。

## Web 工作区

- 流式文本、可折叠思考过程和工具卡，展示实际输入输出。
- Mermaid 代码块可切换图表／源码并复制；点击图表或 Expand 放大，支持缩放、拖动、
  Fit 适应窗口、100% 原始尺寸与 Esc 关闭，配色跟随主题。
- 浏览器审批、Ask / Auto 权限切换、模型切换和上下文读数。
- 会话搜索、重命名、归档、恢复与 Markdown / JSON 导出。
- 粘贴、拖入或附加 PNG / JPEG / GIF / WebP 图片，每条消息最多四张、每张 5 MiB。
- DeepDocx 可用聊天附件做以图搜图／图文检索；工具卡展示少量来源与覆盖，模型按需
  回读原文和原图。侧栏“语义索引 x/y”显示当前可用覆盖。
- 编辑重发、重新生成、独立回退对话或代码；恢复前可查看文件冲突与 diff。
- 停止当前任务、压缩上下文，以及 `Ctrl/⌘ + /` 快捷键面板。

Write / Edit 在操作前保存文件检查点。恢复时核对当前文件与操作后的 hash，手工修改
形成冲突并需要显式强制。Bash 的文件、进程或外部副作用无法靠检查点恢复。
Fork 到 DeepDocx 的继承历史只读，回滚边界从进入新会话后开始。

默认地址为 `127.0.0.1:8765`。非 loopback 监听会自动要求 token，也可以通过 `--token`
显式设置。页面采用 CSP，API 使用 `X-DCX-Token`，并兼容旧 `X-CLI-Agent-Token`
请求头；新请求头存在时优先验证它。跨网络使用时配合认证代理。

## 文档与索引管理

进入 DeepDocx 后，侧栏的 **添加文档** 用于上传并生成 bundle，**索引管理** 用于
构建／同步多模态索引、选择 caption、停止索引任务及试查文字／图片。两种任务各自
在后台运行，关闭弹窗不停止；停止对话回合也不等于取消后台入库或索引任务。

CLI 提供同样的系统管理入口，不创建会话，也不需要回答模型：

```bash
dcx docs ingest --library /path/to/paper-bundles --mode basic paper.pdf
dcx docs index status --library /path/to/paper-bundles
dcx docs index sync --library /path/to/paper-bundles --no-wait
dcx docs index sync --library /path/to/paper-bundles --doc paper_37126553647c
dcx docs index cancel --library /path/to/paper-bundles JOB_ID
dcx docs index search --library /path/to/paper-bundles "记忆模块与动作预测"
dcx docs index search --library /path/to/paper-bundles --image figure.png
```

基础转换不需要模型密钥；语义同步和查询需要配置外部推理服务密钥。`sync` 默认等待
完成，`--no-wait` 返回任务记录；状态命令可继续查看进度。`--force` 会重新请求推理，
日常新增文档使用普通同步即可。首次手动同步会为该库启用新文档入库后的自动同步。

入库、索引同步命令以 0 表示成功，部分完成／失败／取消为 1；与下方 `dcx run` 的
Agent 回合退出码分开理解。完整参数见 `dcx docs --help` 和 `dcx docs index --help`，
工作流见[文档入库](deepdocx-ingest.md)与[多模态索引](multimodal-index.md)。

## 权限与工具

| 模式 | 行为 |
|---|---|
| `default` | 工作区内读取直接执行，其他操作需要审批 |
| `accept-edits` | 工作区内读取和文件写入直接执行，Shell 等操作需要审批 |
| `bypass` | 跳过普通审批；静态高风险删除保护仍适用 |

allow / deny 规则可进一步控制权限，deny 优先；显式 allow 会优先于危险命令提示。
`always` 按工具记住本会话的允许决定。权限规则是审批机制，不能替代操作系统沙箱。

内置工具为 `Bash`、`Read`、`Write`、`Edit`、`Glob`、`Grep`、`load_skill`、
`DiscoverTools`、`CallExternalTool`；启用记忆后提供 `Remember`。
连续的安全读取可以并行，写入和 Shell 按序执行，权限提示始终串行。

非交互模式的审批默认拒绝。需要执行命令时可以明确使用 allow 规则或 bypass：

```bash
dcx run --permission-mode bypass "运行测试并总结结果"
```

## 终端内命令

| 命令 | 用途 |
|---|---|
| `/help` | 查看命令 |
| `/clear` | 清空当前历史与压缩检查点，用量继续累计 |
| `/compact` | 压缩较早上下文 |
| `/model` | 查看或切换模型 |
| `/auto [on\|off]` | 切换完整权限模式 |
| `/memory [list\|forget NAME]` | 查看或删除长期记忆 |
| `/cost` | 用量、上下文估算和缓存命中率 |
| `/exit` | 退出 |

## JSON 与 JSONL 输出

```bash
printf '总结这个仓库' | dcx run --output-format jsonl -
dcx run --output-format json "介绍模块结构"
```

`text` 输出回复文本；`json` 输出单个结果；`jsonl` 实时逐行输出事件。
stdout 只承载数据，诊断信息写 stderr。

JSONL 事件：`session_start`、`text`、`reasoning`、`tool_started`、`tool_finished`、
`usage`、`notice`、`context`、`error`、`result`。思考事件需要 `--thinking`。
最终 `result` 包含 session、provider、model、回复、工具调用、用量、通知、错误，以及
`stop_reason` / `is_complete` 完成状态。

退出码：`0` 为 `end_turn`；`2` 为错误或不完整回合；`1` 为配置/初始化错误；
`130` 为中断。部分输出后的重试带 `partial` 标记，消费端应区分尝试与完整结果。

Web SSE 沿用流式文本和工具事件，并有 `turn_start`、`permission_request`、`retry`、
`cancelled`、`done`。外部 MCP 调用继续通过现有 runtime 和事件流执行。
