# cli-agent

A small, provider-neutral CLI coding agent.

`cli-agent` is built around one agent loop, explicit tool and permission boundaries,
durable sessions, context compaction, long-term memory, and a modular system prompt.
It is provider-neutral: the Anthropic-compatible adapter works with Anthropic and
compatible endpoints such as DeepSeek, Kimi, MiniMax, and GLM.

## Design

```
user input
  -> context pipeline (repair, spill, prune, snip, summarize)
  -> provider.stream()
  -> text deltas / tool calls
  -> tool executor (validate -> permission -> run)
  -> tool results appended
  -> repeat until the model stops calling tools
```

One `AgentRuntime` owns the transcript, the model, and usage. The CLI only reads input,
renders events, and calls the runtime.

## Install

```bash
uv sync
uv run cli-agent --help
```

## Configure a provider

Providers are explicit; `cli-agent` never guesses from the model name.

```bash
export DEEPSEEK_API_KEY=...
uv run cli-agent --provider deepseek --model deepseek-flash[1M]
```

Or copy [config.example.toml](config.example.toml) to `~/.config/cli-agent/config.toml`:

```toml
default_provider = "deepseek"
default_model = "deepseek-flash[1M]"
permission_mode = "accept-edits"

[providers.deepseek]
kind = "anthropic_compat"
base_url = "https://api.deepseek.com/anthropic"
api_key_env = ["DEEPSEEK_API_KEY"]
default_model = "deepseek-flash[1M]"
context_window = 880000
max_output_tokens = 256000
supports_thinking = true
thinking_by_default = true

[thinking]
# Leave enabled unset to use the provider default (DeepSeek: on).
# enabled = false
budget_tokens = 100000  # must be below max_output_tokens

[memory]
# The agent writes memory through the Remember tool; no background extraction.
enabled = true
```

## Usage

```bash
cli-agent                              # interactive REPL
cli-agent -p "fix the failing test"    # one-shot
cli-agent -c                           # resume the latest session
cli-agent --resume <session-id>
cli-agent --permission-mode default
cli-agent --provider deepseek           # defaults to deepseek-flash[1M] + thinking
cli-agent --provider deepseek --no-extended-thinking
                                        # disable the provider's thinking default
cli-agent --no-thinking                 # hide model reasoning in the REPL
cli-agent --thinking -p "..."           # show reasoning on stderr in print mode
```

`--extended-thinking` is a request capability: it sends the provider's `thinking`
parameter and requires the selected provider/model to declare `supports_thinking`.
Providers may also default it on (`thinking_by_default`); the DeepSeek preset does,
so `cli-agent --provider deepseek` requests thinking without extra flags.
`--thinking/--no-thinking` only controls display, so `--thinking` alone will not make
the model think. Use a reasoning-capable model; for DeepSeek, `deepseek-flash`
(not `deepseek-chat`) produces reasoning.

`-p` exits `0` only when the turn ends normally. A refusal, provider error, or
`--max-turns` cutoff exits `2`, and the stop reason is written to stderr. Retries are
announced too (inline in the REPL, on stderr in print mode); if an attempt had already
streamed text, the notice says that the earlier text is stale rather than silently
splicing two partial answers together.

`/cost` reports billed usage (uncached input, output, cache reads and writes) plus the
estimated size of the last request against the context window. Provider `input_tokens`
excludes the cached prefix, so prompt size is input + cache reads + cache writes; the
token estimator and its calibration use that sum.

Built-in tools: `Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `load_skill`, and
`Remember` when memory is enabled.
`Glob` skips dependency and build directories unless the pattern or `path` names one.
Consecutive read-only calls (`concurrency_safe`) in one turn run in parallel; writes and
shell commands stay ordered barriers, and permission prompts are always serialized.
`/auto [on|off]` toggles full-permission mode and remembers the mode it replaced. The
change is appended to the session, so `-c` restores it (an explicit `--permission-mode`
still wins), and the REPL banner shows the active mode.

Shell commands pass one static guard first: recursive deletes aimed at `/`, `$HOME`, a
parent directory, the current directory, or a system directory always need a fresh
interactive approval — bypass and `/auto` do not skip it, and "always" is never
remembered for them. It is approval UX, not a sandbox: `sh -c`, `xargs`, and scripts are
out of scope, and an explicit `permissions.allow` rule still wins.

Sessions, memory, and session spill files live under
`$XDG_DATA_HOME/cli-agent/projects/<project-id>/`. Project instructions are read from
`AGENTS.md` and `.agents/`; `cli-agent` does not reuse `.claude/` or `CLAUDE.md`.

## Learn

- [01 · 一次流式工具调用：从 SSE 到 ToolResultBlock](learn/01_streaming_tool_call.md)
  —— 模型、API、SSE、SDK、adapter、runtime、ToolExecutor 的完整路径；以及为什么内部
  Block/Delta 不直接使用 SDK 类型。

## System prompt

The prompt is assembled from ordered modules under `src/cli_agent/context/prompts/`:
identity, workspace, tools, file/shell policies, project instructions, memory, skills,
and compaction. The tool list is generated from the live registry, and modules only
claim capabilities the harness actually implements. Modules marked `cacheable` form the
static prefix; when the provider profile sets `prompt_caching = true` (the `anthropic`
preset does) the adapter emits `cache_control` breakpoints for that prefix and the tool
schemas. Third-party Anthropic-compatible endpoints stay off by default because they may
reject the field.

## Status (v1)

Implemented today:

- one async agent loop with streaming text, normalized tool calls, retries,
  `max_tokens` continuation, and cancellation;
- explicit provider abstraction plus the Anthropic-compatible adapter
  (Anthropic, DeepSeek, Kimi, MiniMax, GLM presets);
- `Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `load_skill`;
- image input: `Read` returns image blocks for png/jpeg/gif/webp, and presets
  declaring `supports_images` (`anthropic`, `deepseek`) forward them; other profiles
  receive a text placeholder instead;
- ordered tool execution inside the one loop: permission checks stay serialized, and
  consecutive `concurrency_safe` reads run in parallel while writes and shells act as
  barriers;
- opt-in prompt caching: `cache_control` breakpoints for the static prompt prefix and
  tool schemas on profiles with `prompt_caching = true`;
- permission modes, allow/deny rules, workspace boundary, interactive and
  fail-closed non-interactive asks, and `/auto` for full-permission mode;
- append-only JSONL sessions, `-c/--resume`, transcript repair, context checkpoints;
- ordered context compaction (spill, prune, snip, summarize) and reactive compaction;
  pruning only replaces tool results that are clearly larger than the disk-pointer
  preview, so the request actually shrinks;
- file-backed memory written only by the agent's `Remember` tool (strict admission
  criteria in the tool description and prompt); records are injected into the newest
  user message, so the cached system prefix stays stable. `/memory [list|forget X]`
  inspects or removes them;
- skills from `~/.config/cli-agent/skills` and `.agents/skills`;
- extended thinking requests for providers that support them (Anthropic and DeepSeek
  presets; opt in another provider with `supports_thinking = true`), with provider
  defaults (`thinking_by_default`), budget validation, and signed-reasoning replay
  after tool calls. The DeepSeek preset defaults to `deepseek-flash[1M]` with thinking
  on;
- streaming reasoning display: on by default in the REPL, off in print mode;
  `--thinking/--no-thinking` overrides it (print mode writes reasoning to stderr);
- REPL, `-p/--print`, and `/help /clear /compact /model /auto /memory /cost /exit`.

Deliberately deferred: MCP, external hook processes, subagents, teams, worktrees,
cron/background tasks, workflow runtime, goal loop, web tools, LSP, notebook, plan
mode, tool search, OpenAI-compatible adapter, and a public plugin SDK.

Notes: compaction is best-effort. Permission rules are approval UX,
not a sandbox — use an external sandbox for untrusted work. DeepSeek-compatible
endpoints may emit reasoning even without `--extended-thinking`; the display flag
still controls whether it is shown.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run pytest tests/e2e -q   # skipped unless CLI_AGENT_E2E_PROVIDER is set
uv run ruff check src tests
uv run mypy src
```
