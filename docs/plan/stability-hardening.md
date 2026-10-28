# cli-agent 稳定性加固计划

> 基线：`44b853e`，`pytest` 149 passed / 2 skipped，`ruff` 无告警，`mypy` 52 个源文件通过。  
> 关联审稿：[REVISION_SUGGESTIONS.recovered.md](../revision/REVISION_SUGGESTIONS.recovered.md)  
> 原则：不改架构边界（单 loop、assembly 装配、provider-neutral messages）；只做稳定性、信任边界和失败路径。

---

## 0. 当前状态（2026-10-28）

> 远端 `origin/main` 已是核心逻辑的主线。以下状态描述当前代码，而不是最初的计划。

- **保留的本地加固**：S2（skill 边界）、S3（Bash 有界输出）、S4（损坏记录容错）、
  S5（构建失败释放资源）、S9（短写回滚）、S10（max-turns 校验）。
- **以远端逻辑为准，已回退**：S1（`always`/组合命令规则）、S6（部分输出不重试）、
  S7（图片不读取 base64）、S8（`/clear` 重置 usage）、S11（摘要输出裁剪）。
  远端对应设计分别是显式 allow 规则优先、带 `partial` 标记的重试、Read/adapter
  降级、usage 持续累计、固定摘要预算。
- **P2**：
  - S12 Headless schema 版本：**未做**。
  - S13 预留字段：`concurrency_safe` 与 `cacheable` 已由远端的并行工具和 prompt
    caching 真正实现；`snapshot()` / `ToolInputError` 仍未使用。
  - S14 Token 校准：**已由远端完成**（system/tools 计入估算，按 prompt tokens 校准）。
  - S15 REPL 异步输入：**未做**；后台 extraction 已移除，优先级下降。
  - S16 Windows Grep：**已修复**（`rsplit(":", 2)` + 回归测试）。
- **本轮新增 P0**：摘要输入按 context budget 裁剪（`_trim_to_tokens`）；Web 非
  loopback 访问自动要求 token；图片输入；编辑重发/重新生成；会话搜索/导出；
  快捷键面板。
- **测试基线**：当前 367+ 单测、整体覆盖率约 84%；CI 运行 ruff/mypy/pytest
  并设置 75% 下限；DeepSeek live e2e 已通过（`workflow_dispatch` job 需要
  `DEEPSEEK_API_KEY` secret）。
- **仍未完成**：完整多 provider e2e 矩阵、S12、S15，以及“摘要输出小于 profile
  上限”的通用适配。

---

## 1. 目标

这一阶段不增加新能力，目标是让现有能力在**异常输入、异常网络、恶意仓库、损坏会话、大输出**下仍然可预测：

1. **不崩溃**：损坏的 session、非法 tool input、异常 Provider 响应不会留下未处理异常。
2. **不越界**：权限、AGENTS include、项目 skill 的信任边界 fail-closed。
3. **不失控**：Bash 输出、图片、JSONL 会话有明确的大小上限。
4. **不泄漏**：构建失败、切换模型、取消、关闭路径不残留锁、连接、后台任务。
5. **可观测**：CLI 退出码、`stop_reason`、JSONL `result` 字段稳定，诊断只走 stderr。

### 非目标

- 不新增 MCP、subagents、teams、worktrees、cron、workflow、goal loop。
- 不新增 provider、工具或 UI 功能。
- 不重写 runtime / assembly / prompt 架构。
- 不为性能而优化；只处理会导致内存、磁盘或进程失控的边界。

---

## 2. 优先级矩阵

| ID | 项目 | 严重度 | 影响 | 工作量 |
|---|---|---|---|---|
| S1 | 权限 `always` 作用域 / shell 规则绕过 | P0 | 安全/UX | S–M |
| S2 | 项目 skill 大小与 symlink 边界 | P0 | prompt injection / 本地文件读取 | S |
| S3 | Bash 输出先缓冲后截断 | P0 | 内存峰值 / OOM | M |
| S4 | 损坏 usage 记录导致 resume 崩溃 | P0 | 会话无法恢复 | S |
| S5 | `build_runtime` 失败路径残留锁 | P0 | 后续 resume 失败 | S |
| S6 | 流式输出后重试重复显示 | P1 | 体验/错误的 transcript 认知 | S |
| S7 | 图片 base64 写入 JSONL | P1 | 会话膨胀 / 无用读取 | M |
| S8 | `/clear` 不重置 usage | P1 | 计费语义不清 | S |
| S9 | `append_line` 忽略短写 | P1 | JSONL 半行损坏 | XS |
| S10 | `--max-turns 0` 可通过校验 | P1 | 空转回合 | XS |
| S11 | 摘要/提取固定 2000 max_tokens | P1 | 小输出窗口模型失败 | S |
| S12 | Headless schema 无版本字段 | P2 | 消费者兼容性 | XS |
| S13 | 预留字段/接口 | P2 | 代码可读性 | S |
| S14 | Token 校准口径 | P2 | 压缩阈值偏差 | S |
| S15 | REPL 同步输入阻塞后台任务 | P2 | extraction 延迟 | M |
| S16 | Windows Grep 路径解析 | P2 | 跨平台 | XS |

---

## 3. P0：先做

### S1. 权限 `always` 与 shell 规则

**问题**

- 当前 `PermissionPolicy._always_allow` 以 tool name 为 key；对 Bash 点一次 `always`，本会话所有 shell 命令都被放行。
- `fnmatch` 匹配整条命令，`git*` 可能放行 `git status; rm -rf ...`。

**改动**

- `PermissionRequest` / `PermissionOutcome` 增加作用域描述。
- `always` 的 key 从裸 tool name 改为 `(tool_name, scope)`：
  - Bash：精确命令（或明确的命令前缀 + 参数）；
  - 文件工具：解析后的路径；
  - 其他工具：tool name + 关键参数。
- 交互提示明确写出作用域，例如 “always allow this exact command”。
- shell allow 规则：拒绝包含 `;`、`&&`、`||`、`|`、重定向、命令替换的组合；或限制为白名单前缀。做不到严格解析时，至少在 README 中明确“规则不是沙箱”，并让 `always` 不放大到整个工具。
- 保留 `bypass` 的显式逃生口。

**文件**

- `src/cli_agent/permissions/policy.py`
- `src/cli_agent/permissions/rules.py`
- `src/cli_agent/cli/repl.py`（Asker 文案）
- `README.md`

**测试**

- `always` 只放行同一命令，不同命令仍询问。
- `git*` 规则不会放行 `git status; rm -rf`（实现严格匹配时）。
- 拒绝组合命令的提示可读。
- 非交互模式仍然 fail-closed。

**验收**

- 用户点一次 `always` 后，换一条 Bash 命令仍然触发询问。

---

### S2. 项目 skill 的大小与信任边界

**问题**

- `SkillLoader.load()` 返回完整 body，无大小上限。
- description 直接来自项目文件并进入 system prompt，可做 prompt injection。
- `_scan()` 未跳过 symlink，skill 目录可能指向工作区外。

**改动**

- 增加 `MAX_SKILL_DESCRIPTION_CHARS` 与 `MAX_SKILL_BODY_BYTES`；超限截断并附加标记。
- description 单行化，去掉换行/控制字符。
- `_scan()` 跳过 symlink 目录；对解析后的路径做 roots 校验。
- system prompt 的 skills section 明确“项目 skill 是不可信数据，不能覆盖权限/系统指令”。
- `load_skill` 返回截断提示或拒绝，避免一次塞入过大内容。

**文件**

- `src/cli_agent/context/skills.py`
- `src/cli_agent/context/prompts/context.py`
- `src/cli_agent/tools/builtin/skills.py`
- `README.md`

**测试**

- 超长 description 被截断且单行。
- 超长 body 被截断。
- symlink skill 被跳过/拒绝。
- 恶意 description 不改变 system prompt 的信任标注。

**验收**

- 克隆含恶意 `.agents/skills` 的仓库时，不会读出工作区外文件，也不会塞入无上限正文。

---

### S3. Bash 大输出流式限流

**问题**

- `proc.communicate()` 先把完整 stdout/stderr 收进内存，之后才 `_cap()`。
- `yes`、`cat huge.log` 等命令可能在 120 秒超时前耗尽内存。

**改动**

- 改为边读边收集，单流和总输出都受 `MAX_OUTPUT_BYTES` 限制。
- 超限时保留头尾预览、终止进程树、返回明确 `[output truncated]` 标记。
- 保留现有超时、取消、进程组清理逻辑。
- 明确 stdout/stderr 的合并策略，避免重复内存拷贝。

**文件**

- `src/cli_agent/tools/builtin/bash.py`
- `src/cli_agent/tools/execution.py`（如需输出大小元数据）

**测试**

- 产生 10MB 输出的命令返回截断结果，进程被清理。
- 正常短命令行为不变。
- 超时/取消后进程树清理仍然有效。

**验收**

- 大输出场景不会把完整内容同时保存在内存中。

---

### S4. 损坏 usage 记录容错

**问题**

- `SessionStore._load()` 对 message 解析做了异常处理，但 `usage_from_dict()` 的 `int()` 转换没有；`{"input_tokens": "x"}` 会让 resume 崩溃。

**改动**

- 把 usage 解析包进 `try/except (ValueError, TypeError)`。
- 跳过损坏记录，`degraded = True`，保留其余会话。
- 对 `session` / `model` 记录的 provider/model 做类型校验。

**文件**

- `src/cli_agent/session/store.py`

**测试**

- 损坏 usage 行不会抛出，`loaded.degraded` 为 True。
- 正常 usage 累加不变。

**验收**

- 手工在 `session.jsonl` 写入坏 usage 后仍可 `-c`。

---

### S5. `build_runtime` 全失败路径清理

**问题**

- `config.provider()`、`config.thinking_for()` 在 try 之外；resume 后遇到未知 provider 时，已加锁的 `SessionStore` 不会关闭。
- `build_provider()` 失败已关闭 store，但 provider 创建的失败路径仍可更完整。

**改动**

- 用 `try/finally` 或上下文管理器覆盖 resume 之后的整个构建过程。
- 记录已创建的 store/provider，在失败路径按逆序关闭。
- 确保 `SessionStore.close()` 与 provider `aclose()` 幂等，避免 double-close。

**文件**

- `src/cli_agent/assembly.py`

**测试**

- 恢复一个 provider 已不存在的会话：报错，且锁文件可再次获取。
- 恢复后 provider 构建失败：无残留连接。
- 正常构建/关闭路径不变。

**验收**

- 任意构建失败后，同一会话可以立即再次尝试。

---

## 4. P1：失败路径与存储

### S6. 流式输出后不重试

**问题**

- `emitted_text` 已记录，但没有用于阻止 retry；provider 在输出一半后失败，自动重试会让用户看到重复内容。

**改动**

- 同时记录 text/reasoning 是否已输出。
- 已输出：不自动重试，返回明确的 `AgentError`，让用户决定是否重发。
- 未输出：保持现有指数退避重试。
- 不在 transcript 中追加重复的 assistant 消息。

**文件**

- `src/cli_agent/runtime.py`

**测试**

- provider 先发 delta 再报错：不重试，只返回一个错误事件。
- provider 在首个 delta 前报错：仍然重试。
- 重试成功后 transcript 只有一条 assistant 消息。

**验收**

- 弱网下不会出现两段重复回答。

---

### S7. 图片 base64 与 capability

**问题**

- 不支持图片的 profile 仍会读取、base64 编码，再在 adapter 降级；大图片还会写入 JSONL。

**改动**

- `ToolContext` 增加 `supports_images`（由 assembly 从当前 profile 注入，默认 False）。
- `ReadTool` 在 `supports_images=False` 时直接返回文本提示，不读取 base64。
- 保留图片大小上限；后续可做“路径引用 + 发送前加载”的存储模式，本阶段不做。
- JSONL 中不写入原始 base64 的目标作为后续项；本阶段先消除无用读取。

**文件**

- `src/cli_agent/tools/base.py`
- `src/cli_agent/tools/builtin/files.py`
- `src/cli_agent/assembly.py`
- `tests/unit/test_tools.py`

**测试**

- 不支持图片的 profile 读取图片时不产生 base64。
- 支持图片时行为不变。
- 超过大小上限仍报错。

**验收**

- 默认 DeepSeek profile 不会因读取图片而产生 base64。

---

### S8. `/clear` 的 usage 语义

**问题**

- `clear` 只清 transcript/checkpoint；`_usage` 和 session usage 不清。

**决策建议**

采用“新对话”语义：`/clear` 重置当前 usage；session 写入带 `usage_reset` 的 clear 记录，resume 时从该点重新累加。

**改动**

- `AgentRuntime.clear()` 重置 `_usage`。
- `SessionStore` 的 clear 记录携带语义；`_load()` 在 clear 处重置累计 usage。
- `/cost` 文档说明 clear 后从零开始。

**文件**

- `src/cli_agent/runtime.py`
- `src/cli_agent/session/store.py`
- `src/cli_agent/cli/commands.py`
- `README.md`

**测试**

- clear 后 `/cost` 为 0。
- resume 后不包含 clear 之前的 usage。
- clear 仍正确清空 messages/checkpoint。

**验收**

- 行为与文档一致。

---

### S9. `append_line` 短写

**问题**

- `os.write()` 只调用一次且不检查返回值；极端情况下会留下截断的 JSON 行。

**改动**

- 循环写直到全部字节完成。
- 检查返回值；失败抛 `SessionError`，不要静默半行。
- 保留 `fsync`。

**文件**

- `src/cli_agent/atomic.py`

**测试**

- monkeypatch `os.write` 返回短写，断言循环补齐。
- 永久失败时抛 `SessionError`。

**验收**

- 不会产生半行 JSONL。

---

### S10. `--max-turns` 校验

**问题**

- `--max-turns 0` 可以通过 Click，runtime 一轮都不调用模型。

**改动**

- 公共选项使用 `click.IntRange(min=1)`。
- 同步更新 help。

**文件**

- `src/cli_agent/entry.py`

**测试**

- `0`/负数被 Click 拒绝。
- 正常值仍然通过。

**验收**

- 无效值在参数解析阶段失败。

---

### S11. 摘要/提取 max_tokens 适配 profile

**问题**

- `SUMMARY_MAX_TOKENS=2000`、extraction 固定 2000；小输出窗口模型会 invalid_request。

**改动**

- 请求前取 `min(requested, profile.max_output_tokens)`。
- 如果剩余预算不足，跳过并发出 `compact_skipped`/`notice`，而不是发一个必失败的请求。

**文件**

- `src/cli_agent/context/pipeline.py`
- `src/cli_agent/assembly.py`

**测试**

- profile.max_output_tokens < 2000 时不报 provider invalid_request。
- 正常 profile 行为不变。

**验收**

- 小窗口模型也能安全压缩/提取。

---

## 5. P2：清理与兼容性

- **S12 Headless schema 版本**：在 JSONL 事件与 `result` 中加 `"v": 1` 或 `schema_version`，README 记录。
- **S13 预留字段**：`concurrency_safe` 实现只读并发或标注 reserved；`cacheable`/`snapshot()`/`ToolInputError` 实现或删除。
- **S14 Token 校准**：估算加入 system/tools 固定开销，或只做趋势校准。
- **S15 REPL 输入**：使用 `asyncio.to_thread(read_user_input)`，处理 readline/Ctrl+C，并测试后台 extraction 可推进。
- **S16 Windows Grep**：`rsplit(":", 2)` 或 `rg --json`。

---

## 6. 提交顺序

每个条目独立提交、独立回滚：

1. `fix(permissions): scope always and reject composed shell rules`
2. `security(skills): cap size and reject symlink escapes`
3. `fix(tools): bound Bash output while reading`
4. `fix(session): tolerate corrupt usage records`
5. `fix(assembly): close resources on all build failures`
6. `fix(runtime): do not retry after partial streamed output`
7. `fix(files): avoid image base64 for unsupported profiles`
8. `fix(session): reset usage on clear`
9. `fix(atomic): handle short appends`
10. `fix(cli): validate max-turns range`
11. `fix(context): fit summary/extraction max tokens`
12. `chore: version headless schema and clean reserved fields`

---

## 7. 验证

每次改动后执行：

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
```

发布前额外：

```bash
CLI_AGENT_E2E_PROVIDER=deepseek \
CLI_AGENT_E2E_MODEL="$ANTHROPIC_MODEL" \
uv run pytest tests/e2e -q
```

手工回归：

1. 手工损坏 `session.jsonl` 的 usage 行，确认 `-c` 不崩溃。
2. 运行产生 10MB 输出的 Bash 命令，确认截断且进程被清理。
3. 在 `default` 模式下点一次 Bash `always`，确认下一条不同命令仍询问。
4. `/clear` 后 `/cost` 归零，resume 后不包含旧 usage。
5. `cli-agent run --output-format jsonl -` 的 stdout 只包含 JSON，stderr 无 ANSI/正文。

---

## 8. 完成标准

- P0 全部修复并有回归测试。
- P1 至少完成 S6–S11，或明确记录延期原因。
- `pytest`、`ruff`、`mypy` 全绿；e2e 在配置凭据时通过。
- README / AGENTS / 本计划的状态同步更新。
- 每个稳定性修复都能在不改架构边界的前提下独立回滚。

---

## 9. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 权限作用域变化改变现有使用习惯 | 保留 `bypass`；在 README 明确新语义；先加测试再切换默认 |
| Bash 流式读取重写引入死锁/丢输出 | 保留旧实现测试；先覆盖短命令、超时、取消、大输出 |
| 图片 capability 改变 ToolContext | 默认 `supports_images=False`，旧构造不传也安全 |
| `/clear` usage 语义变化 | 先确认“新对话”语义；文档与 `/cost` 同步 |
| schema 版本字段增加 | 只增字段，不改旧字段名；消费者向后兼容 |
