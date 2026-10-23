# 01 · 一次流式工具调用：从 SSE 到 `ToolResultBlock`

[返回 cli_agent](../README.md) · 基础篇 · 约 20 分钟

> **本章的一句话**
>
> 模型在远端只生成 token；服务商的 API/服务层把模型输出组织成 content block 并编码为 SSE 帧；本地 SDK 把收到的 SSE 帧解析成本地事件对象；adapter 把厂商事件翻译成内部事件；runtime 把 `tool_use` 变成一次本地工具执行，再把 `tool_result` 作为下一次请求发回去。

---

## 0. 先问三个问题

如果你刚读完 `learn-claude-code` 的 s01–s03，可能已经会写一个能调用工具的 agent 了。但有几个问题，代码能跑不代表心里真的清楚：

1. 模型输出一个 `tool_use` 块的时候，Bash 是在远端的 GPU 集群上跑的吗？
2. 工具执行的结果，是通过同一条流式连接发回给模型的吗？
3. 为什么 `cli_agent` 要自己定义 `ToolUseBlock` / `ToolResultBlock`，而不直接使用 Anthropic SDK 已经写好的类型？

这一章会把这三件事讲透。读完之后，你应该能只看一段 agent 代码，就指出：

- 哪一层在生成 token；
- 哪一层在解析流；
- 哪一层在翻译格式；
- 哪一层在执行工具；
- 哪一层在管理对话状态。

---

## 1. 概念地图

### 1.1 你已经知道的

你的认知是对的：

```
远端 GPU 集群
  └─ vLLM / SGLang
       └─ 收到请求，逐 token decode
            └─ 把生成的 token 返回给你的机器
```

这一章要补的是：**在“token”与“你的 agent 代码”之间，还隔着几层。**

### 1.2 一张术语表

| 词 | 一句话解释 | 谁负责 |
|---|---|---|
| **模型 / inference** | 根据输入逐 token 生成输出 | 服务商的 GPU 集群 |
| **API** | 一个 HTTP 契约：你发 JSON，服务端按约定回 JSON | 服务商与你的进程之间 |
| **Stateless** | 服务端不记得上一轮对话；每次都要重发完整 `messages` | API 的约定 |
| **Streaming** | 服务端不等全部生成完，而是边生成边返回 | HTTP 响应的传输方式 |
| **SSE** | Server-Sent Events：一种“服务器持续向客户端推文本事件”的 HTTP 响应格式 | 流式传输的具体线格式 |
| **SDK** | 一个**本地**客户端库，替你处理 HTTP、认证、解析收到的 SSE 帧、类型转换、重试 | 你的机器上的 `anthropic` 包 |
| **Wire format** | 网络上真正传输的 JSON 形状 | SSE 的 `data:` 里 |
| **Domain model** | 我们自己定义的内部类型：`Message`、`Block`、`ToolCall` | `cli_agent` |
| **Adapter** | 把厂商 wire format 翻译成内部 domain model 的那一层 | `providers/anthropic_compat.py` |
| **Harness** | 组装请求、执行工具、管会话、管权限的代码 | `AgentRuntime` + tools + permissions + session |
| **Tool call** | 模型生成、由 API 层组织成 `tool_use` block 的“请执行这个工具”请求；它**没有执行**任何东西 | 模型生成 / 服务商 API 结构化 |
| **Tool result** | Harness 执行完后回填给模型的结构化结果；和 tool call 用 id 配对 | Harness |

先记住这一句：

> **模型没有手。它只是写了一张工单；真正干活的是你的 harness。**

### 1.3 分层图

```
┌────────────────────────────────────────────────────────┐
│ 你的机器                                                │
│                                                        │
│  CLI / REPL                                            │
│    │ AgentEvent                                        │
│  AgentRuntime            ← 只有一个循环                 │
│    │ ModelRequest                                      │
│  Provider adapter        ← 厂商格式与内部格式的边界     │
│    │ ProviderEvent                                     │
│  Anthropic SDK           ← 本地：HTTP / SSE 帧解析      │
│    │                                                    │
│  ToolExecutor（Bash/Read/Write…）← 工具在你这里执行      │
│  SessionStore / ContextPipeline ← 状态在你这里          │
└────┼───────────────────────────────────────────────────┘
     │
     │  HTTPS POST，响应是 SSE
     ▼
┌────────────────────────────────────────────────────────┐
│ 服务商                                                  │
│  GPU → 推理引擎（vLLM / SGLang）                        │
│       → API/服务层：组织成 content block + SSE 帧       │
└────────────────────────────────────────────────────────┘
```

一眼要看出两件事：

1. **工具执行发生在你的机器上**，不在 GPU 集群上。
2. **SDK 在 adapter 下面**，不是内核的一部分；更不是模型的一部分。

### 1.4 谁把 token 变成事件？

这里有**两次不同的“解析”**，必须分开：

| 阶段 | 在哪里 | 输入 | 输出 | 谁负责 |
|---|---|---|---|---|
| 模型推理 | 远端 GPU | prompt tokens | output tokens（原始生成） | 模型 / 推理引擎 |
| API 结构化 | 远端服务层 | 模型输出 + 协议 | content block、`tool_use` 的 id/name/input、SSE 事件帧 | 服务商的 API / 推理服务层 |
| 传输 | 网络 | SSE 帧 | HTTP chunk / 字节流 | HTTP |
| 帧解析 | 本地进程 | SSE 字节流 | 本地事件对象（`TextEvent`、`InputJsonEvent` 等） | Anthropic SDK |
| 最终 message 累积 | 本地进程 | 本地事件 / delta | 最终 `Message` | Anthropic SDK |
| 归一化 | 本地进程 | 厂商事件 / message | `TextDelta` / `ToolCall` / `ResponseCompleted` | 我们的 adapter |

关键句：

> **远端负责“把模型输出组织成 API 语义结构，并编码成 SSE 帧”。**
>
> **本地 SDK 负责“把收到的 SSE 帧解析成本地事件对象”。**
>
> **SDK 解析的是 SSE 帧，不是 token 流。客户端通常根本看不到原始 token 流。**

不同推理栈的内部分工可能不同：有的模型直接生成结构化 token/JSON，服务层负责抽取；有的服务层做更多后处理。对客户端来说，边界始终是 **API 事件**，而不是原始 token。

这也是为什么文档里说“模型生成 token”，但不说“SDK 解析 token”：token 流在远端就被服务层结构化成了 API 事件；本地收到的是事件帧。

---

## 2. SSE 到底是什么

### 2.1 一句话

SSE（Server-Sent Events）是一种 **HTTP 响应体格式**：

- Content-Type 是 `text/event-stream`；
- 连接建立后，服务器可以持续往客户端写事件；
- 每个事件由 `event:`、`data:` 和空行组成；
- 它是**单向的**：服务器 → 客户端。

### 2.2 它不是什么

- **不是 WebSocket**。SSE 不能双向通信；tool result 不会从同一条连接回去。
- **不是模型**。它只是传输格式；模型在远端只负责生成 token。
- **不是“服务器记住了你”**。API 是 stateless 的；每次请求都要带上完整历史。

### 2.3 一个原始 SSE 片段

下面是一个**示意**的流（字段名以实际 API 为准）：

```text
event: message_start
data: {"type":"message_start","message":{...}}

event: content_block_start
data: {"type":"content_block_start","index":1,
       "content_block":{"type":"tool_use","id":"toolu_01ABC","name":"Bash","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,
       "delta":{"type":"input_json_delta","partial_json":"{\"command\":\"echo "}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,
       "delta":{"type":"input_json_delta","partial_json":"hello\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":23}}

event: message_stop
data: {"type":"message_stop"}
```

要看懂三件事：

1. **事件是分块的**。一个 HTTP chunk 可能包含半个事件，也可能包含多个事件；本地 SDK 会替你缓冲字节流，并按 SSE 帧解析。
2. **工具参数是 JSON 碎片**。`input_json_delta` 里的 `partial_json` 是逐 token 生成的字符串，拼起来才是完整的 `{"command": "..."}`。
3. **`content_block_stop` 才代表一个块结束**。文本块、工具块各自有 index，各自结束。

### 2.4 为什么要有流式

如果不用流式，服务端会：

```
收到请求 → 生成完整回复 → 一次性返回一个 JSON
```

用户会一直等到全部生成完才看到第一个字。流式的好处是：

- 更快的首字延迟；
- 可以实时显示“正在生成”；
- 工具调用可以尽早被观察到；
- 长时间生成时，连接不会显得“卡死”。

### 2.5 关键洞察

> **SSE 是单向的。**  
> 工具结果不会从这条连接发回去；它是**下一次全新的 HTTP 请求**。

这一点是很多人第一次接触 agent 时最容易误解的地方。

---

## 3. 一个 streaming tool call 的完整时序

例子：用户说 **“创建 hello.py 并运行它。”**

### t0：发出请求

`AgentRuntime` 组装 `ModelRequest`，adapter 转成 wire JSON，本地 SDK 发出 HTTPS 请求：

```http
POST /v1/messages
x-api-key: ...
anthropic-version: ...
content-type: application/json

{
  "model": "...",
  "max_tokens": 8192,
  "stream": true,
  "system": "...",
  "tools": [
    {"name": "Bash", "description": "...", "input_schema": {...}}
  ],
  "messages": [
    {"role": "user", "content": "创建 hello.py 并运行它"}
  ]
}
```

注意两点：

- **工具定义每次都要发**。服务端不保存你的工具表。
- **messages 是完整的**。API 是 stateless 的，不会“接着上次的对话”。

### t1–t4：模型先输出文本

```text
message_start          → 只有 input token 统计
content_block_start    → index=0, type=text
content_block_delta    → text_delta: "I'll create "
content_block_delta    → text_delta: "the file."
content_block_stop     → index=0 结束
```

adapter 把 `text_delta` 翻译成内部 `TextDelta`；runtime 再翻译成 `AgentTextDelta`；CLI 实时打印。

### t5：模型决定调用工具，API 输出 `tool_use` 块

```text
content_block_start
data: {"index":1,"content_block":{"type":"tool_use","id":"toolu_01ABC","name":"Bash","input":{}}}
```

这一刻模型只是**声明**：

> “我想调用 Bash，id 是 `toolu_01ABC`。”

它没有运行任何命令。

### t6：工具参数逐段到达

```text
content_block_delta
data: {"index":1,"delta":{"type":"input_json_delta","partial_json":"{\"command\":\"printf "}}

content_block_delta
data: {"index":1,"delta":{"type":"input_json_delta","partial_json":"'print(\\\"hello\\\")' > hello.py\"}"}}
```

拼起来才是：

```json
{"command": "printf 'print(\"hello\")' > hello.py"}
```

**不要自己逐段 `json.loads`。** 这些碎片可能在任意位置断开；让本地 SDK 累积，等 `content_block_stop` 或最终 message。

### t7：块结束，adapter 发出 `ToolCall`

在我们的 adapter 里，`content_block_stop` 拿到的是 **本地 SDK 已经累积并解析好的 block**：

```python
yield ToolCall(
    tool_use_id="toolu_01ABC",
    name="Bash",
    input={"command": "printf ... > hello.py"},
    parse_error=None,
)
```

`ToolCall` 是**观察事件**，用于 UI 显示“> Bash ...”。runtime 不会拿它直接执行；它只记录 `parse_error`，真正执行用后面的最终 message。

### t8：响应结束，得到权威消息

```text
message_delta   → stop_reason="tool_use", output_tokens=...
message_stop    → 流结束
```

adapter 调用 `await stream.get_final_message()`，得到本地 SDK 累积出的完整 message，然后：

- `_final_blocks()` → 内部 `TextBlock` + `ToolUseBlock`
- `_final_usage()` → 内部 `Usage`
- `_stop_reason()` → 内部 `StopReason`
- 发出 `ResponseCompleted(message, stop_reason, usage)`

runtime 拿到的是：

```python
Message(
    role="assistant",
    content=(
        TextBlock("I'll create the file."),
        ToolUseBlock(id="toolu_01ABC", name="Bash", input={"command": "..."}),
    ),
)
```

> **为什么不用 `ToolCall` 直接执行？**
>
> 因为流可能中断、JSON 可能被截断、本地 SDK 累积出的最终消息才是完整且经过累积校验的。
>
> **流式事件用于展示，最终 message 用于行动。**

### t9：runtime 追加 assistant 消息

runtime 把这条 assistant message 追加到 transcript，同时写入 session：

```python
self._append(assistant)
```

此时 transcript 是：

```
user:      创建 hello.py 并运行它
assistant: [text, tool_use(id=toolu_01ABC)]
```

**assistant 必须先入 transcript**，因为下一条 user 消息里的 `tool_result` 必须和它配对。

### t10：runtime 执行工具

```python
calls = assistant.tool_uses()
for call in calls:
    result = await self._deps.executor.execute(
        call,
        ctx=...,
        policy=...,
        asker=...,
        parse_error=parse_errors.get(call.id),
    )
```

`ToolExecutor.execute` 的顺序是：

```
1. parse_error?        → 返回错误 ToolResult
2. registry.get(name)  → 未知工具 → 错误 ToolResult
3. validate_tool_args  → 参数不符合 JSON Schema → 错误 ToolResult
4. policy.authorize    → 权限拒绝 → 错误 ToolResult
5. tool.run(...)       → 真正执行 Bash/Read/Write...
6. 异常                → 错误 ToolResult
```

**工具在你的机器上执行**，不在 GPU 集群上。Bash 的进程、文件系统、权限都在本地。

### t11：把结果包装成 `ToolResultBlock`

```python
ToolResultBlock(
    tool_use_id="toolu_01ABC",
    content=(TextBlock("hello\n"),),
    is_error=False,
)
```

然后 runtime 把它放进一条 **user message**：

```python
Message(
    role="user",
    content=(ToolResultBlock(...),),
)
```

并追加到 transcript，同时写入 `session.jsonl`。

### t12：下一次请求，把 tool result 发回去

adapter 把内部消息转成 wire：

```json
{
  "messages": [
    {"role": "user", "content": "创建 hello.py 并运行它"},
    {"role": "assistant", "content": [
      {"type": "text", "text": "I'll create the file."},
      {"type": "tool_use", "id": "toolu_01ABC", "name": "Bash",
       "input": {"command": "printf ... > hello.py"}}
    ]},
    {"role": "user", "content": [
      {"type": "tool_result", "tool_use_id": "toolu_01ABC",
       "content": [{"type": "text", "text": "hello\n"}],
       "is_error": false}
    ]}
  ]
}
```

模型在下一次请求中看到结果，继续生成：

```text
content_block_delta → "Created and ran hello.py. Output: hello"
message_delta       → stop_reason="end_turn"
message_stop
```

runtime 发现 `assistant.tool_uses()` 为空，循环结束，发出 `AgentTurnFinished("end_turn")`。

### 时序总表

| 时刻 | 发生了什么 | 谁的职责 |
|---|---|---|
| t0 | 发出第一次 POST，带 messages + tools | runtime → adapter → 本地 SDK |
| t1 | `message_start`，input token 统计 | 本地 SDK → adapter |
| t2–t4 | 文本 delta | adapter → runtime → UI |
| t5 | `content_block_start`：tool_use id/name | 本地 SDK → adapter |
| t6 | `input_json_delta`：参数碎片 | 本地 SDK 累积 |
| t7 | `content_block_stop`：adapter 发 `ToolCall` | adapter |
| t8 | `message_stop` → 最终 message → `ResponseCompleted` | 本地 SDK → adapter |
| t9 | assistant message 入 transcript | runtime |
| t10 | 本地执行工具 | ToolExecutor |
| t11 | `ToolResultBlock` 入 user message | runtime |
| t12 | 第二次 POST，带 tool_result | runtime → adapter → 本地 SDK |
| t13 | 模型收尾，`end_turn` | 服务商 |

> **一个 tool call = 至少两次 HTTP 请求。**
>
> 第一次请求工具，第二次把结果送回去。

---

## 4. 代码逐站对照

### 4.1 Adapter：流式事件与最终消息

[`AnthropicCompatProvider.stream`](../src/cli_agent/providers/anthropic_compat.py#L101) 是整条线上唯一知道 Anthropic wire format 的地方：

```python
async with self._client.messages.stream(**params) as stream:
    async for event in stream:
        event_type = getattr(event, "type", "")

        if event_type == "content_block_delta":
            delta = getattr(event, "delta", None)
            if delta is None:
                continue
            if delta.type == "text_delta":
                yield TextDelta(delta.text)
            elif delta.type == "thinking_delta":
                yield ReasoningDelta(delta.thinking)
            continue

        if event_type == "content_block_stop":
            block = getattr(event, "content_block", None)
            if block is not None and block.type == "tool_use":
                yield ToolCall(
                    tool_use_id=block.id,
                    name=block.name,
                    input=dict(block.input),
                    parse_error=None,
                )
            continue

    final = await stream.get_final_message()

# ... _final_blocks / _final_usage / _stop_reason ...
yield ResponseCompleted(message=..., stop_reason=..., raw_stop_reason=..., usage=...)
```

要点：

- `input_json_delta` **没有被显式处理**。本地 SDK 会累积 JSON 碎片；我们等 `content_block_stop` / 最终 message 拿完整 `input`。
- 最终 message 是行动的真相；流式 delta 是展示的真相。
- 本地 SDK 还会额外产出 `TextEvent`、`ThinkingEvent`、`InputJsonEvent`、`SignatureEvent` 等便利事件；我们**只处理 raw 的 `content_block_delta` / `content_block_stop`**，避免重复计算。

### 4.2 Runtime：从事件到工具执行

[`AgentRuntime.run_turn`](../src/cli_agent/runtime.py#L132) 消费 adapter 的事件：

```python
async for provider_event in self._deps.provider.stream(request):
    if isinstance(provider_event, TextDelta):
        yield AgentTextDelta(provider_event.text)
    elif isinstance(provider_event, ToolCall):
        yield AgentToolStarted(...)
    elif isinstance(provider_event, ResponseCompleted):
        assistant = provider_event.message
        self._append(assistant)
        ...
        yield AgentUsage(...)

calls = assistant.tool_uses()
if not calls:
    ...
    return

for call in calls:
    result = await self._deps.executor.execute(...)
    results.append(_to_result_block(call.id, result))

self._append(tool_result_message(results))
```

关键设计：

- **以 `assistant.tool_uses()` 是否存在为执行依据**，而不是只看 `stop_reason`。
- assistant 消息先入 transcript，再执行工具；这样 `tool_use` / `tool_result` 的配对顺序永远正确。
- 如果执行中取消，runtime 会为未完成的调用合成 `[Tool call cancelled by the user]` 结果，保证 transcript 仍是 API-valid 的。

### 4.3 Executor：validate → authorize → run

[`ToolExecutor.execute`](../src/cli_agent/tools/execution.py#L21) 把所有失败都变成 `ToolResult`：

```python
if parse_error:
    return ToolResult.error(...)

tool = self.registry.get(call.name)
if tool is None:
    return ToolResult.error("unknown tool ...")

validation_error = validate_tool_args(tool.spec, call.input)
if validation_error:
    return ToolResult.error(...)

outcome = await policy.authorize(tool.spec, call.input, asker)
if not outcome.allowed:
    return ToolResult.error("Permission denied: ...")

try:
    return await tool.run(call.input, ctx)
except Exception as exc:
    return ToolResult.error(f"{type(exc).__name__}: {exc}")
```

工具不会因为“参数错了”“没权限”“抛异常”而让循环崩溃；模型会在下一条 tool_result 里看到错误，并决定怎么改。

### 4.4 Session：结果进入持久化

runtime 的 `_append` 会把每条消息写入 [SessionStore](../src/cli_agent/session/store.py#L44)：

```python
def _append(self, message: Message) -> None:
    self._deps.transcript.append(message)
    self._deps.session.append_message(message)
```

`session.jsonl` 里只存内部 `Message` 的 JSON，不存 SDK 类型。恢复时由 `message_from_dict` 反序列化，再用 `Transcript.for_request()` 做一次 API 安全性修复。

---

## 5. 为什么 `ToolCall` 和 `ToolResultBlock` 是两种东西

它们属于三个不同层：

```
模型请求          ToolCall（provider 层，瞬时观察）
        │
        ▼
最终消息          ToolUseBlock（domain 层，assistant 的 content）
        │
        ▼
Harness 执行      ToolResult（工具返回值）
        │
        ▼
回填给模型        ToolResultBlock（domain 层，user 的 content）
```

| 类型 | 角色 | 是否持久化 | 谁创建 |
|---|---|---|---|
| `ToolCall` | 流式观察事件，给 UI / parse_error 用 | 否 | adapter |
| `ToolUseBlock` | 权威的模型工具请求 | 是 | adapter（从最终 message 转换） |
| `ToolResult` | 工具执行层的统一返回 | 否 | tool / executor |
| `ToolResultBlock` | 回填给模型的 tool 结果 | 是 | runtime |

配对键是 `tool_use_id`：

```
assistant: [tool_use id="toolu_01ABC", name="Bash", input={...}]
user:      [tool_result tool_use_id="toolu_01ABC", content=[...], is_error=false]
```

`is_error=true` 时也一样配对。权限拒绝、参数非法、工具异常，都是“结果”，不是“异常终止”。

> **模型永远看不到我们的 Python 对象。**
>
> 它看到的是下一次请求里的 `tool_use` + `tool_result` JSON。

---

## 6. 为什么自定义 Block / Delta，而不用 SDK 类型

这是本章最容易被误解的一点。先直接回答：

> **多 provider 兼容是结果之一，不是根因。根因是：会话是产品资产，SDK 类型是实现细节。**

### 6.1 SDK 类型面比我们需要的“大得多”

你环境里的 `anthropic 2.1.0`：

| | Anthropic SDK | `cli_agent` 内部模型 |
|---|---|---|
| 内容块 | **12 种**：`TextBlock`、`ThinkingBlock`、`RedactedThinkingBlock`、`ToolUseBlock`、`ServerToolUseBlock`、`WebSearchToolResultBlock`、`WebFetchToolResultBlock`、`CodeExecutionToolResultBlock`、`BashCodeExecutionToolResultBlock`、`TextEditorCodeExecutionToolResultBlock`、`ToolSearchToolResultBlock`、`ContainerUploadBlock` | **5 种**：[TextBlock](../src/cli_agent/messages.py#L22)、[ReasoningBlock](../src/cli_agent/messages.py#L27)、[ImageBlock](../src/cli_agent/messages.py#L40)、[ToolUseBlock](../src/cli_agent/messages.py#L46)、[ToolResultBlock](../src/cli_agent/messages.py#L53) |
| `Usage` 字段 | **10 个**：`cache_creation`、`cache_creation_input_tokens`、`cache_read_input_tokens`、`inference_geo`、`input_tokens`、`output_tokens`、`output_tokens_details`、`server_tool_use`、`service_tier`、`speed` | **4 个**：[input / output / cache_read / cache_write](../src/cli_agent/messages.py#L84) |
| `Message` 字段 | **11 个**：`id`、`container`、`content`、`diagnostics`、`model`、`role`、`stop_details`、`stop_reason`、`stop_sequence`、`type`、`usage` | **4 个**：[id / role / content / created_at](../src/cli_agent/messages.py#L64) |

我们的模型是一次**投影**，不是 SDK 的镜像。内核只理解自己会“行动”的部分。

### 6.2 四个决定性理由

#### ① 会话是持久化格式，不能绑死 SDK 版本

`session.jsonl` 里保存的是我们的 `message_to_dict()` 结果，不是 Pydantic 对象：

- [store.py:152](../src/cli_agent/session/store.py#L152) `message_to_dict`
- [store.py:260](../src/cli_agent/session/store.py#L260) `message_from_dict`

如果直接存 SDK 类型：

- SDK 从 2.1 升到 3.x，字段改名/增删，旧会话可能无法反序列化；
- Pydantic 的 `model_dump()` 输出会随版本变化；
- `thinking` 的 `signature`、`redacted_thinking` 的 `data` 等 provider 内部 opaque 数据会泄漏进公共 schema。

现在我们的规则是：**磁盘格式由 `messages.py` 的显式序列化决定，SDK 升级不影响历史会话。**

#### ② 内核只应该理解自己会“行动”的部分

我们真正会处理的内容块只有五类。SDK 新增一个 `ContainerUploadBlock` 时，adapter 选择忽略，而不是让 runtime 多一个分支。

同理，stop_reason 被收敛成 6 个值（[`_stop_reason`](../src/cli_agent/providers/anthropic_compat.py#L45)），未知值统一变成 `error` + `raw_stop_reason`。runtime 不需要知道 Anthropic 的 `stop_sequence`、`refusal`、`content_filter` 细节。

#### ③ 测试不应该依赖某个 SDK 的构造复杂度

loop 测试用 [FakeProvider](../tests/support/fake_provider.py) 直接产出内部 `TextDelta` / `ResponseCompleted`，不需要构造 `anthropic.types.Message` 的 11 个字段。这让“循环行为”可以脱离网络和 SDK 版本单独测试。

#### ④ 多协议兼容是结果，不是原因

即使永远只接 Anthropic，上面三点仍然成立。而一旦要接 OpenAI-compatible，内部模型的必要性就变成硬性的：

| 概念 | Anthropic | OpenAI-compatible |
|---|---|---|
| 工具调用 | `tool_use` block，`input` 是对象 | `tool_calls[].function`，`arguments` 是 JSON 字符串 |
| 流式文本 | `content_block_delta` / `text_delta` | `choices[].delta.content` |
| 停止原因 | `end_turn` / `tool_use` / `max_tokens` | `stop` / `tool_calls` / `length` |
| usage | `input_tokens` / `output_tokens` | `prompt_tokens` / `completion_tokens` |
| thinking | `thinking` / `signature` | 没有同构概念 |

如果内核直接说 Anthropic 的类型，加 OpenAI 时就要改 runtime、context、session、测试。现在只需要新增 `providers/openai_compat.py`，实现同一个 `Provider.stream()`。

### 6.3 Delta 和 Block 的生命周期不同

```
SSE content_block_delta ──► TextDelta / ReasoningDelta / ToolCall ──► AgentEvent ──► 渲染器
final message           ──► Message(TextBlock/ToolUseBlock/...)   ──► transcript / session
```

| | Delta | Block |
|---|---|---|
| 生命周期 | 瞬时 | 持久 |
| 用途 | UI 流式展示 | transcript / 会话恢复 / 压缩 / 记忆 |
| 是否可切分 | 可以任意切分 | 完整单元 |
| provider 差异 | 最大 | 被 adapter 归一化 |

所以：

- `TextDelta` 不需要写入 `session.jsonl`；
- `ToolCall` 只是观察事件；
- 只有最终 `Message` 里的 `Block` 才进入持久化。

### 6.4 我们并没有“重造 SDK”

这条线不是“所有东西都自己写”。我们**复用了 SDK 最值钱的部分**：

- `AsyncAnthropic` 的 HTTP、认证、解析收到的 SSE 帧；
- `messages.stream()` 和 `get_final_message()` 的流式累积；
- `APIStatusError` / `APIConnectionError` / `APITimeoutError`，再由 [`_classify_error`](../src/cli_agent/providers/anthropic_compat.py#L57) 映射成 `ProviderError`。

我们**没有**重写 HTTP、SSE、JSON 解析、重试、认证。adapter 里真正做映射的只有：

- [`_messages_to_wire`](../src/cli_agent/providers/anthropic_compat.py#L177)
- [`_content_to_wire`](../src/cli_agent/providers/anthropic_compat.py#L189)
- [`_final_blocks`](../src/cli_agent/providers/anthropic_compat.py#L275)
- [`_final_usage`](../src/cli_agent/providers/anthropic_compat.py#L308)
- `_stop_reason` / `_classify_error`

### 6.5 这条边界怎么判定

三条实用规则：

1. **会写进 session 文件的类型，必须是内部类型。**
2. **会出现在 runtime/context/tools/permissions 签名里的类型，必须是内部类型。**
3. **vendor 包的类型只能出现在 `providers/` 里。**

实际检查：

```bash
grep -rn "import anthropic" src/cli_agent
# src/cli_agent/providers/anthropic_compat.py:14:import anthropic
# src/cli_agent/providers/anthropic_compat.py:15:from anthropic import AsyncAnthropic
```

只有 adapter 一处。`runtime.py`、`session/store.py`、`context/`、`tools/`、`permissions/` 都没有 import Anthropic。

这条规则可以直接写成测试：断言 `runtime/session/context/tools/permissions` 的模块不包含 `anthropic`。这样它就从“约定”变成“CI 保证”。

### 6.6 代价与何时不值得

这个设计有代价：

- 多了一层映射代码（adapter 里大约 150 行）；
- SDK 新增能力时，需要决定是否加进内部模型；
- 内部模型可能落后于 provider 的新特性（比如 server tool use、container）。

如果项目**永远只接一个 provider、不持久化会话、不做多宿主**，直接用 SDK 类型确实更省事。

但 `cli_agent` 的定位是“通用 CLI agent + 可恢复会话 + 未来多 provider”，所以内部模型是产品契约，SDK 是可替换的实现细节。

---

## 7. 元认知：哪些必须懂，哪些可以当黑盒

### 7.1 三层理解

```
第 1 层：契约层  —— 必须懂
  stateless、content blocks、tool_use 是请求、tool_result 是回答、id 配对

第 2 层：传输层  —— 够用即可
  HTTP、SSE、chunk、本地 SDK 的 SSE 帧解析、流式事件

第 3 层：实现层  —— 可以当黑盒
  tokenizer、KV cache、GPU batching、HTTP/2、TCP 重传
```

### 7.2 必须懂 vs 黑盒

**必须懂（否则会设计错）：**

1. API 是 stateless 的：完整 `messages` 每次重发。KV cache 是性能优化，不是对话记忆。
2. 响应是 content blocks：文本、thinking、tool_use 都只是块；`tool_use` 是“请求”，不是“执行”。
3. `tool_result` 是 user-side 数据：放在 user message 里，和 tool_use 用 id 配对。
4. 流式是增量的：工具参数可能分片到达；必须等块结束/最终消息再解析。
5. 模型没有你的文件系统：Bash 在你机器上跑，权限也在你这边。
6. 工具定义每次都要发：模型不保存你的工具表。

**可以当黑盒（不影响你做对设计）：**

- SSE 的字节级分帧、HTTP/2、TCP 重传；
- SDK 内部的 Pydantic 解析、认证、重试；
- 精确的 tokenizer、GPU batching、KV cache 实现；
- 每家厂商的具体 JSON 字段名（adapter 负责）。

---

## 8. 六个常见误解

| 误解 | 实际情况 |
|---|---|
| “SSE 是双向的，工具结果从同一条连接回去” | SSE 单向；tool result 走新的 POST |
| “模型执行了 Bash” | 模型只输出 `tool_use`；Harness 执行 |
| “SDK 就是模型 / SDK 就是 API” | SDK 是**本地**客户端库；API 是契约；模型是远端 token 生成器 |
| “服务端记得上下文” | 不记得；每次重发完整 messages |
| “tool_use 的 input 一次到齐” | 通常是 JSON 碎片，本地 SDK 累积后给出完整对象 |
| “看 stop_reason 就够了” | 还要看有没有 `tool_use` block；runtime 以 block 是否存在为执行依据 |

---

## 9. 动手实验

### 实验 A：看 raw SDK 事件

前置条件：环境里有 `DEEPSEEK_API_KEY`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`。如果同时设置了 `ANTHROPIC_AUTH_TOKEN`，先 `unset ANTHROPIC_AUTH_TOKEN`，避免本地 SDK 优先使用 bearer token。

在 `/tmp` 下建一个脚本，观察本地 SDK 到底吐出了哪些事件类型：

```python
import asyncio
import os

from anthropic import AsyncAnthropic

TOOLS = [{
    "name": "Bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


async def main() -> None:
    client = AsyncAnthropic(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.environ["ANTHROPIC_BASE_URL"],
        max_retries=0,
    )
    try:
        async with client.messages.stream(
            model=os.environ["ANTHROPIC_MODEL"],
            max_tokens=300,
            tools=TOOLS,
            messages=[{"role": "user", "content": "Use Bash to run: echo trace-me"}],
        ) as stream:
            async for event in stream:
                print("EVENT:", getattr(event, "type", "?"))
            final = await stream.get_final_message()
        print("stop_reason:", final.stop_reason)
        print("blocks:", [block.type for block in final.content])
        print("usage:", final.usage.input_tokens, final.usage.output_tokens)
    finally:
        await client.close()


asyncio.run(main())
```

你会看到 `content_block_start`、`content_block_delta`、`content_block_stop`、`message_delta` 等事件，以及本地 SDK 额外产出的 `text` / `input_json` / `thinking` 等便利事件。

### 实验 B：看内部事件

用 `cli_agent` 自己的 runtime 观察归一化后的事件：

```python
import asyncio
import os

from cli_agent.assembly import BuildOptions, build_runtime


async def main() -> None:
    bundle = build_runtime(
        BuildOptions(
            provider="deepseek",
            model=os.environ["ANTHROPIC_MODEL"],
            persist=False,
            memory_enabled=False,
            permission_mode="bypass",
        )
    )
    try:
        async for event in bundle.runtime.run_turn(
            "Use Bash to run: echo trace-me. Then report the output."
        ):
            print(type(event).__name__, event)
    finally:
        await bundle.runtime.close()


asyncio.run(main())
```

观察点：

1. `ToolCall` / `AgentToolStarted` 出现在 `ResponseCompleted` 之前还是之后？
2. Bash 什么时候真正执行？
3. `AgentToolFinished` 里有没有 `tool_use_id`？
4. transcript 里 assistant 和 user 的顺序是什么？
5. 第二次请求的 `messages` 里是否同时有 `tool_use` 和 `tool_result`？

### 实验 C：看 transcript

在 REPL 里跑一次工具调用，然后执行 `-c` 恢复；打开对应项目的 `session.jsonl`，找到 `"type":"message"` 的记录，观察：

```json
{"role":"assistant","content":[
  {"type":"text","text":"..."},
  {"type":"tool_use","id":"...","name":"Bash","input":{...}}
]}
{"role":"user","content":[
  {"type":"tool_result","tool_use_id":"...","content":[{"type":"text","text":"..."}],"is_error":false}
]}
```

这就是“配对”的物理形态。

---

## 10. 自检

<details>
<summary>1. 为什么 <code>tool_result</code> 放在 <code>role="user"</code> 的消息里？</summary>

因为从 API 的视角看，工具结果是“用户侧”提供的数据：assistant 发出 `tool_use` 请求，user 侧把执行结果送回去。协议用 `tool_use_id` 把两者配对。

</details>

<details>
<summary>2. 为什么 runtime 要等 <code>ResponseCompleted</code> 才执行工具，而不是收到 <code>ToolCall</code> 就执行？</summary>

因为流可能中断，`input_json_delta` 可能只到了一半；`ToolCall` 只是观察事件。最终 message 是完整、经过本地 SDK 累积和校验的，才是可执行的权威请求。

</details>

<details>
<summary>3. <code>tool_use_id</code> 在配对什么？如果丢失会发生什么？</summary>

配对 assistant 的 `tool_use` 和 user 的 `tool_result`。如果 ID 丢失或不匹配，API 可能拒绝请求；`Transcript.for_request()` 会为孤立的 `tool_use` 合成错误的 `tool_result`，保证 transcript 仍然 API-valid。

</details>

<details>
<summary>4. 如果网络在 <code>input_json_delta</code> 中途断开，应该执行工具吗？</summary>

不应该。此时 `ResponseCompleted` 没有到达，runtime 会走错误路径，不执行任何工具。这正是“最终 message 用于行动”的安全意义。

</details>

<details>
<summary>5. 为什么模型每次都要收到完整的 <code>messages</code> 和 <code>tools</code>？</summary>

因为 API 是 stateless 的；服务端不保存你的对话状态和工具表。KV cache 只是性能优化，不是语义上的记忆。

</details>

<details>
<summary>6. 如果明天接 OpenAI-compatible，哪一层需要改？哪一层不需要？</summary>

只需要新增 `providers/openai_compat.py`，把 OpenAI 的 wire format 翻译成同一组 `TextDelta` / `ToolCall` / `ResponseCompleted`。runtime、context、session、tools、permissions 不需要改。

</details>

---

## 11. 本章小结

把整条路径压缩成一句话：

> 模型在远端逐 token 生成 → 服务商的 API/服务层把模型输出组织成 content block，并编码为 SSE 帧 → 本地 SDK 把收到的 SSE 帧解析成本地事件对象，并累积成最终 message → adapter 把厂商事件翻译成 `TextDelta` / `ToolCall` / `ResponseCompleted` → runtime 把 `ToolUseBlock` 交给本地 `ToolExecutor` 执行 → 结果包装成 `ToolResultBlock`，作为下一条 user message 发回去 → 模型继续。

再压缩成 three-line mental model：

```
模型只生成 token。
工具在你的机器上执行。
每次工具调用，都是两次 HTTP 请求。
```

---

## 12. 下一篇

建议按这个顺序继续：

1. **02 · 权限门禁**：从 s03 的 `check_permission` 到 `cli_agent` 的 `PermissionPolicy` —— 为什么权限不该是循环里的一个 `bool`。
2. **03 · 上下文压缩**：从 s08 的 `prepare_context` 到 `ContextPipeline` —— 为什么压缩是“有序管线 + 检查点”，而不是“删几条消息”。
3. **04 · 会话恢复**：JSONL、锁、transcript 修复，以及 `-c` 背后的状态所有权。

如果你想先动手，最好的练习是：**给 `cli_agent` 增加一个自定义工具**，只实现 `ToolSpec` + `run()`，然后观察它如何自动进入 adapter 的 schema、executor 的校验、权限策略和事件流。
