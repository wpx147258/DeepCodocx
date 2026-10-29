<!-- Generated language panels. Edit this file's default-open language, then run python scripts/sync_doc_languages.py. -->

<details name="deepcodocx-doc-language" lang="zh-CN">
<summary>简体中文</summary>

<!-- doc-language:zh-CN:start -->
# CLI 与 Web 使用指南

[首页](../README.md)

## 三个入口

```bash
uv run cli-agent --provider deepseek                    # 交互式终端
uv run cli-agent web --provider deepseek                # 本地 Web
uv run cli-agent run --provider deepseek "介绍这个仓库"   # 一次性执行
```

公共选项可以放在子命令前后。运行 `cli-agent --help`、`cli-agent web --help` 或
`cli-agent run --help` 查看完整参数。Provider 配置见[配置指南](configuration.md)。

## 会话与连续工作

```bash
cli-agent -c                         # 最近的普通会话
cli-agent --resume SESSION_ID
cli-agent --resume SESSION_ID --fork
cli-agent run -c "继续完成剩下的工作"
cli-agent --mode deepdocx -c          # 最近的文档会话
```

显式 provider/model 覆盖保存的设置；仅更换 provider 时采用新 provider 的默认模型。
保存的权限模式随会话恢复，显式 `--permission-mode` 优先。

Fork 复制历史、用量和会话资源，继续写入新 session。普通会话与 DeepDocx 会话分别
展示；进入文档模式可以选择 fork 或全新开始。文档操作见[文档指南](deepdocx.md)。

## Web 工作区

- 流式文本、可折叠思考过程和工具卡，展示实际输入输出。
- 浏览器审批、Ask / Auto 权限切换、模型切换和上下文读数。
- 会话搜索、重命名、归档、恢复与 Markdown / JSON 导出。
- 粘贴、拖入或附加 PNG / JPEG / GIF / WebP 图片，每条消息最多四张、每张 5 MiB。
- 编辑重发、重新生成、独立回退对话或代码；恢复前可查看文件冲突与 diff。
- 停止当前任务、压缩上下文，以及 `Ctrl/⌘ + /` 快捷键面板。

Write / Edit 在操作前保存文件检查点。恢复时核对当前文件与操作后的 hash，手工修改
形成冲突并需要显式强制。Bash 的文件、进程或外部副作用无法靠检查点恢复。
Fork 到 DeepDocx 的继承历史只读，回滚边界从进入新会话后开始。

默认地址为 `127.0.0.1:8765`。非 loopback 监听会自动要求 token，也可以通过 `--token`
显式设置。页面采用 CSP，API 使用 `X-CLI-Agent-Token`。跨网络使用时配合认证代理。

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
cli-agent run --permission-mode bypass "运行测试并总结结果"
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
printf '总结这个仓库' | cli-agent run --output-format jsonl -
cli-agent run --output-format json "介绍模块结构"
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
<!-- doc-language:zh-CN:end -->

</details>

<details name="deepcodocx-doc-language" lang="en" open>
<summary>English</summary>

<!-- doc-language:en:start -->
# CLI and Web guide

[Home](../README.en.md)

## Three entry points

```bash
uv run cli-agent --provider deepseek                    # Interactive terminal
uv run cli-agent web --provider deepseek                # Local Web interface
uv run cli-agent run --provider deepseek "Describe this repository"  # One shot
```

Common options can appear before or after the subcommand. Run `cli-agent --help`,
`cli-agent web --help`, or `cli-agent run --help` for all options. Provider setup
is covered in the [configuration guide](configuration.en.md).

## Sessions and continuing work

```bash
cli-agent -c                         # Latest general session
cli-agent --resume SESSION_ID
cli-agent --resume SESSION_ID --fork
cli-agent run -c "Continue the remaining work"
cli-agent --mode deepdocx -c          # Latest document session
```

Explicit provider/model choices override saved settings. Changing only the
provider selects the new provider's default model. A resumed session restores
its permission mode; an explicit `--permission-mode` takes precedence.

Forking copies history, usage, and session resources, then continues in a new
session. General and DeepDocx sessions have separate history lists. Entering
document mode offers a fork or a fresh start. See the
[document guide](deepdocx.en.md) for document workflows.

## The Web workspace

- Streaming text, collapsible reasoning, and tool cards with actual inputs and
  outputs.
- Browser approvals, Ask / Auto permissions, model switching, and context
  readouts.
- Session search, rename, archive, restore, and Markdown / JSON export.
- Paste, drop, or attach PNG / JPEG / GIF / WebP images: up to four per message,
  5 MiB each.
- Edit and resend, regenerate, and rewind conversation or code independently,
  with file conflicts and diffs available before restoration.
- Stop the current task, compact context, and view shortcuts with `Ctrl/⌘ + /`.

Write / Edit save file checkpoints before making changes. Restoration checks
the current file against its post-operation hash. Manual changes produce a
conflict and require an explicit force decision. File checkpoints cannot undo
the file, process, or external side effects of Bash commands.

History inherited by a DeepDocx fork is read-only. Rewind starts at the new
session's fork boundary.

The default address is `127.0.0.1:8765`. Non-loopback binds automatically require
a token; `--token` sets one explicitly. The page uses CSP, and API requests use
`X-CLI-Agent-Token`. Pair network exposure with an authenticated proxy.

## Permissions and tools

| Mode | Behavior |
|---|---|
| `default` | Reads inside the workspace run directly; other operations require approval |
| `accept-edits` | Reads and file writes inside the workspace run directly; shell and other operations require approval |
| `bypass` | Skips ordinary approvals; the static high-risk deletion guard still applies |

Allow / deny rules provide additional controls. Deny rules take precedence;
explicit allow rules also take precedence over the dangerous-command prompt.
`always` remembers an approval by tool for the current session. Permission
rules are an approval mechanism, not an operating-system sandbox.

Built-in tools are `Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `load_skill`,
`DiscoverTools`, and `CallExternalTool`, plus `Remember` when memory is enabled.
Consecutive safe reads can run in parallel. Writes and shell commands remain
ordered, and permission prompts are always serialized.

Non-interactive approval requests are denied by default. To run commands, use
explicit allow rules or choose bypass:

```bash
cli-agent run --permission-mode bypass "Run the tests and summarize the results"
```

## Terminal commands

| Command | Purpose |
|---|---|
| `/help` | Show commands |
| `/clear` | Clear history and its compaction checkpoint; usage remains cumulative |
| `/compact` | Summarize older context |
| `/model` | Inspect or switch the model |
| `/auto [on\|off]` | Toggle full-permission mode |
| `/memory [list\|forget NAME]` | Inspect or remove long-term memory |
| `/cost` | Show usage, context estimates, and cache hit rates |
| `/exit` | Exit |

## JSON and JSONL output

```bash
printf 'Summarize this repository' | cli-agent run --output-format jsonl -
cli-agent run --output-format json "Describe the module structure"
```

`text` emits assistant text, `json` emits one result object, and `jsonl` streams
events one per line. stdout carries data only; diagnostics go to stderr.

JSONL events are `session_start`, `text`, `reasoning`, `tool_started`,
`tool_finished`, `usage`, `notice`, `context`, `error`, and `result`. Reasoning
events require `--thinking`. The final `result` includes the session, provider,
model, reply, tool calls, usage, notices, errors, and the completion fields
`stop_reason` / `is_complete`.

Exit codes: `0` for `end_turn`; `2` for an error or incomplete turn; `1` for a
configuration or initialization error; `130` for interruption. A retry after
partial output is marked `partial`; consumers should distinguish an attempt's
output from a complete result.

Web SSE includes streaming text and tool events, with `turn_start`,
`permission_request`, `retry`, `cancelled`, and `done`. External MCP calls use
the same runtime and event stream.
<!-- doc-language:en:end -->

</details>
