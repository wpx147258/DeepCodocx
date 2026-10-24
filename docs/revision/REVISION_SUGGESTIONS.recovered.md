# cli-agent 修订建议（更新版）

> 更新日期：2026-10-24  
> 当前基线：`pytest` **149 passed / 2 skipped**；`ruff` 无告警；`mypy` **52 个源文件**通过。  
> 当前 HEAD：`44b853e`（feat: add run subcommand and headless JSON output）。  
> 原始审稿基线：107 passed / 2 skipped、44 个源文件。  
> 原则：不改架构边界（单 loop、assembly 装配、provider-neutral messages），优先修行为错误，再补信任边界。

---

## 0. 当前结论

- **已修复**：P1.1（resume 覆盖）、P1.2（provider slot）、P1.3（stop_reason）、P2.1（AGENTS include 越界），以及 P2.7（异常写 stdout）。
- **仍建议优先**：
  1. 2.8 权限 `always` 作用域 / glob 规则绕过；
  2. 2.2 项目 skill 的大小与信任边界；
  3. 2.3 Bash 大输出内存峰值；
  4. 2.5 流式输出后重试导致重复显示。
- **其余 P2/P3** 多为产品取舍或低优先级清理，可排期。
- **Headless 新审查**：`run` 子命令、JSON/JSONL 输出已实现；未发现阻塞性 bug，但有几条 schema/退出码约定需要保持稳定。

---

## 1. 已修复

| 条目 | 状态 | 提交 | 回归测试 |
|---|---|---|---|
| P1.1 resume 显式参数被覆盖 | 已修复 | `2987f5e` | `tests/unit/test_resume.py`（3 个） |
| P1.2 `/model` 后后台仍用旧 provider | 已修复 | `2987f5e` | `tests/unit/test_provider_slot.py`（2 个） |
| P1.3 stop_reason 被吞 | 已修复 | `2987f5e` | `tests/unit/test_runtime.py` |
| P2.1 AGENTS `@include` 越界 | 已修复 | `2987f5e` | `tests/unit/test_prompt.py` |
| P2.7 one-shot 异常写 stdout | 已修复 | `44b853e` | `tests/unit/test_cli.py` |

### 1.1 resume 显式 provider/model 优先

当前实现（`src/cli_agent/assembly.py:175-188`）：

```text
显式 CLI provider/model > 会话记录 > 配置默认值
```

如果只切换 provider、不指定 model，则使用新 provider 的默认模型，不再把旧会话的 model 带过去。  
测试覆盖：`--model` 覆盖、`--provider` 覆盖、无覆盖沿用会话。

### 1.2 后台压缩/记忆提取跟随当前 provider

- `ProviderSlot` 定义在 `src/cli_agent/runtime.py:70`。
- `complete()` 由 `_make_completer(slot)` 创建（`src/cli_agent/assembly.py:93`），每次调用读取 `slot.provider/profile`。
- `switch_model()` 同步更新 slot。
- 因此 `/model` 后 `/compact`、memory extraction 不再使用已 `aclose()` 的旧 provider。

### 1.3 保留真实 stop_reason

当前实现（`src/cli_agent/runtime.py:268-290`）：

- `refusal` → `AgentError(kind="refusal")`，最终 `AgentTurnFinished("refusal")`；
- `error` → `AgentError(kind="error")`，最终 `stop_reason="error"`；
- `max_tokens` 连续耗尽 → `AgentNotice("Output limit reached...")`，最终 `stop_reason="max_tokens"`；
- 只有真正的 `end_turn` 会触发 memory extraction。

### 1.4 AGENTS include 限制在允许根目录

当前实现（`src/cli_agent/context/prompts/instructions.py:39-73`）：

- 项目 `AGENTS.md` / `.agents/rules/*.md` 的 `@include` 只允许工作区内文件；
- 全局配置 `AGENTS.md` 的 include 只允许配置目录内；
- 越界输出 `[include denied: outside <root>: <path>]`，不读取内容；
- `resolve()` 后使用 `is_relative_to(root)`，symlink 逃逸同样被拒绝。

测试覆盖 `@../secret.txt`、`@/etc/passwd`、全局 include 越界。

### 1.5 one-shot 诊断不再污染 stdout

`PrintRenderer.fatal()` 在 `src/cli_agent/cli/render.py:171` 改为写 stderr；`run_print` 捕获未预期异常后调用它。  
text 模式 stdout 只保留 assistant 文本。

---

## 2. 仍待处理：P2

### 2.1 项目 skill 的 description/body 信任过度

位置：`src/cli_agent/context/skills.py:46`（load）、`:55`（_scan）、`:62`（glob）、`:70`（description）。

风险仍然存在：

- 项目 `.agents/skills` 由仓库控制，description 可做 prompt injection；
- 单个 skill body 无大小上限，`load_skill` 会把全文塞进上下文；
- skill 目录若是指向外部的 symlink，可能读取工作区外文件。

建议：

- 增加 `MAX_SKILL_DESCRIPTION_CHARS`、`MAX_SKILL_BODY_BYTES`，超限截断并标记；
- 在 system prompt 中标注项目 skill 为不可信数据；
- `_scan()` 跳过 symlink 目录，或校验解析后路径在允许 roots 内；
- description 单行化，避免伪造 system prompt 结构。

### 2.2 Bash 输出上限是事后截断

位置：`src/cli_agent/tools/builtin/bash.py:85`。

`proc.communicate()` 先把完整 stdout/stderr 收进内存，之后才 `_cap()`（`:96`）。  
`yes`、`cat huge.log` 等命令在 120 秒超时前可能耗尽内存。

建议改为边读边收集，超过 `MAX_OUTPUT_BYTES` 时保留头尾预览、终止进程树并返回截断标记；保留现有超时/取消清理逻辑。

### 2.3 图片和 reasoning 写入 JSONL 导致会话膨胀

位置：`src/cli_agent/tools/builtin/files.py:109-113`（图片 base64）、`messages.py` 的序列化、`session/store.py` 的逐条写入。

- 5MB 图片变成约 6.7MB base64；多次读取会让 session 文件迅速膨胀；
- 不支持图片的 profile 仍会先读取并 base64 编码，再在 adapter 降级；
- reasoning signature 是 thinking 回放所必需的，不能简单截断。

建议优先做“不支持图片的 profile 不读取 base64”；图片引用模式可以后续再做。

### 2.4 重试会重复显示已流式输出的内容

位置：`src/cli_agent/runtime.py:213-251`。

`emitted_text` 已记录，但没有用于阻止重试；`ProviderFailure` 后仍会 `_recover → retry`，之前已经 yield 的文本无法撤回。

建议：

- 记录本轮是否已经输出 text/reasoning；
- 已输出时不再自动重试，返回明确错误；
- 未输出时继续指数退避重试。

### 2.5 `/clear` 不重置 usage

位置：`src/cli_agent/runtime.py:339-344`、`session/store.py:265`。

`clear` 只清 transcript 和 checkpoint；`_usage` 不清，session 的 usage 累加也不重置。

需要产品决策：

- “新对话”语义：重置 usage，并在 clear 记录中表达；
- 或“清屏但保留账单”：在 `/cost` 和文档中写清楚。

推荐前者。

### 2.6 权限 `always` 是 tool 级，allow glob 可被 shell 续写绕过

位置：`src/cli_agent/permissions/policy.py:64`、`:125-126`。

- 对 Bash 点一次 `always`，等于本会话所有 shell 命令放行；
- `fnmatch` 匹配整条命令，`git*` 可能放行 `git status; rm -rf ...`。

建议至少把 `always` 的提示/作用域改成“精确命令/路径”，并在 README 中明确规则不是安全边界。更严格的方向是限制可解析命令前缀或禁止组合符。

### 2.7 `build_runtime` 部分失败路径未释放锁

位置：`src/cli_agent/assembly.py:192-205`。

`build_provider` 失败会关闭 store；但 `config.provider()`、`config.thinking_for()` 在 `try` 之前，resume 后遇到未知 provider 时仍可能残留已加锁的 `SessionStore`。

建议用 `try/finally` 或上下文管理器覆盖整个构建过程，并确保 close 幂等。

---

## 3. 仍待处理：P3

### 3.1 `concurrency_safe` 未使用

`ToolSpec.concurrency_safe` 在 `src/cli_agent/tools/base.py:22` 声明，但 runtime 仍串行执行。  
建议实现只读工具并发，或删除/标注 reserved，避免误导。

### 3.2 预留字段/接口

- `SystemSection.cacheable`：adapter 未发 `cache_control`；
- `ToolRegistry.snapshot()`（`src/cli_agent/tools/registry.py:30`）：目前只有文档用途；
- `ToolInputError`（`src/cli_agent/errors.py:18`）：定义但未使用。

建议实现或标注 reserved。

### 3.3 Token 校准口径不一致

`src/cli_agent/context/tokens.py:62` 用“仅 messages 的估算”对比 provider 的 `input_tokens`，后者通常包含 system、tools 等固定开销。  
建议估算加入固定开销，或只用同一口径做趋势校正。

### 3.4 REPL 输入阻塞事件循环

`src/cli_agent/cli/repl.py:59` 直接调用同步 `read_user_input()`，用户输入期间后台 extraction 无法推进。  
可改为 `asyncio.to_thread`，但需处理 readline 与 Ctrl+C。

### 3.5 损坏的 usage 记录会让 resume 崩溃

`src/cli_agent/session/store.py:265` 的 `usage_from_dict()` 未捕获 `ValueError/TypeError`。  
建议降级 `degraded=True` 并跳过该条。

### 3.6 Windows 下 Grep 路径解析

`src/cli_agent/tools/builtin/search.py:219` 使用 `line.split(":", 2)`，Windows 盘符路径会切错。  
建议 `rsplit(":", 2)` 或 `rg --json`。

### 3.7 `append_line` 忽略短写

`src/cli_agent/atomic.py:33-40` 只调用一次 `os.write()` 且不检查返回值。  
建议循环写直到完成，失败抛 `SessionError`。

### 3.8 `--max-turns 0` 可通过 Click 校验

`src/cli_agent/entry.py:38` 仍是 `type=int`。建议 `click.IntRange(min=1)`。

### 3.9 摘要/提取的 max_tokens 固定 2000

`SUMMARY_MAX_TOKENS = 2_000`（`src/cli_agent/context/pipeline.py:45`）、extraction 固定 `2_000`（`src/cli_agent/assembly.py:270`）。  
小输出窗口模型可能收到 invalid_request。建议 `min(requested, profile.max_output_tokens)` 或跳过并发 `compact_skipped`。

### 3.10 Headless schema 版本化（新增建议）

`JsonRenderer` 的 JSONL/result schema 目前没有版本字段。若外部消费者会长期依赖，建议增加稳定的 `"v": 1` 或 `schema_version`，并写入 README。

---

## 4. Headless / CLI 新审查

已实现（`44b853e`）：

- 默认 `cli-agent` 进入 REPL；
- `cli-agent run ...` 为 one-shot；
- `--output-format text|json|jsonl`；
- 公共选项可在 `run` 前后；
- stdout 只放数据，诊断/异常走 stderr；
- 退出码：`0` 完成、`2` agent error/不完整回合、`1` 配置/会话错误、`130` 中断；
- `run` 需要 prompt 或 `-`（stdin）。

结论：**未发现阻塞性 bug**。需要保持的约定：

1. JSONL 每行一个对象，事件随到随写；
2. `text` 事件是增量 token，消费者需要拼接；
3. `reasoning` 只在 `--thinking` 时出现；
4. `result` 必须包含 `session_id`、`provider`、`model`、`stop_reason`、`is_error`、`is_complete`、`usage`；
5. `tool_calls` 在 result 中包含 `tool_use_id`、`name`、`input`、`is_error`、`content`；
6. 异常/中断也要产出 `error` + `result`（JSONL），text 模式写 stderr；
7. 不把 Rich/ANSI 输出混进 JSON；
8. 未来增加 schema 版本字段，避免消费者被字段变更破坏。

一个已知取舍：`run` 没有交互 asker，Bash 在 `default`/`accept-edits` 下会 fail-closed；headless 任务需要 `--permission-mode bypass` 或 allow 规则。

---

## 5. 建议顺序

1. **2.6 权限作用域**：安全/UX 风险最高，且改动局部。
2. **2.1 skill 大小与 symlink 边界**：与 AGENTS include 同一类信任问题。
3. **2.2 Bash 输出限流**：防内存峰值，中等工作量。
4. **2.4 重试重复输出**：体验问题，改动小。
5. **2.3 图片存储**：产品设计，先做 capability 检查。
6. **P3 便宜项**：3.5 usage 容错、3.7 短写、3.8 IntRange、3.10 schema 版本。

---

## 6. 提交拆分建议

已落地：

- `2987f5e fix: honor resume overrides and harden runtime boundaries`
- `44b853e feat: add run subcommand and headless JSON output`

后续建议独立提交：

1. `security(prompt): cap skills and reject symlink escapes`
2. `fix(tools): bound Bash output while reading`
3. `fix(runtime): don't retry after partial streamed output`
4. `fix(session): tolerate corrupt usage records`
5. `fix(cli): validate max-turns range`
6. `chore: implement or mark reserved fields`
7. `fix(session): avoid persisting image base64 when unsupported`

每项带对应回归测试。

---

## 7. 测试矩阵（更新）

已覆盖：

| 区域 | 测试 |
|---|---|
| resume | `tests/unit/test_resume.py`：显式 model/provider 覆盖、无覆盖沿用 |
| provider slot | `tests/unit/test_provider_slot.py`：slot 替换、switch_model 同步 |
| stop reason | `tests/unit/test_runtime.py`：refusal/error/max_tokens |
| AGENTS include | `tests/unit/test_prompt.py`：越界、全局根目录 |
| headless | `tests/unit/test_json_output.py`、`tests/unit/test_cli.py`：JSON/JSONL、错误退出、stderr 隔离 |

仍建议补充：

- skill 超长 description/body、symlink；
- Bash 大输出/超时/取消；
- 权限 `always` 作用域、组合命令；
- 损坏 usage、clear usage；
- Windows Grep 路径；
- `append_line` 短写；
- 摘要/提取小 max_output_tokens。

---

## 8. 不要做的事

- 不要新增第二个 agent loop；所有回合逻辑继续留在 `runtime.py`。
- 不要把 Anthropic/DeepSeek 的 wire format 带进 runtime 或 messages。
- 不要从模型名前缀推断 provider；继续显式选择。
- 不要把 session/memory/spill 写进项目目录；继续使用 XDG data 目录。
- 不要把 permission 规则描述成沙箱；当前设计明确是审批 UX。
- 不要在修 P2 的同时大改架构；先补回归测试，再做小范围重构。
- 不要把 headless JSON 的字段/事件名随意改名；它是公开消费面。

---

## 9. 验收命令

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
```

当前结果：

```text
149 passed, 2 skipped
ruff: All checks passed
mypy: Success, 52 source files
```

补充要求：

- `tests/e2e` 在没有 `CLI_AGENT_E2E_PROVIDER` 时仍然 skip，不意外联网；
- 新增测试全部使用 `tests/support/fake_provider.py` 或纯本地文件，不依赖真实 API；
- 每个 P2/P3 修复都有对应的失败前/通过后测试。

---

## 10. 一句话总结

**P1 三条主路径与 AGENTS include 信任边界已经修好；下一阶段优先补权限作用域、skill 信任和 Bash 大输出。headless JSON/JSONL 已可用，接下来主要是稳定 schema 和补边角测试。**
