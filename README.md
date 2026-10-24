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
enabled = true
extraction_min_new_messages = 8   # require at least this many new visible messages
extraction_cooldown_seconds = 60  # minimum gap between extraction attempts
```

## Headless output

`cli-agent run` is the one-shot headless mode. Use `run -` to read the prompt
from stdin. `--output-format` selects the output shape:

| Format | stdout |
|---|---|
| `text` (default) | assistant text only |
| `json` | one JSON result object |
| `jsonl` | one JSON object per line, emitted as events arrive |

stdout carries data only; logs and diagnostics go to stderr.

```bash
printf "Summarize this repo" | cli-agent run --output-format jsonl -
cli-agent run --permission-mode bypass --output-format json "Run the tests"
```

JSONL event types:

```text
session_start, text, reasoning, tool_started, tool_finished,
usage, notice, context, error, result
```

`reasoning` is included only with `--thinking`. The final `result` object includes
`session_id`, `provider`, `model`, `resumed`, `stop_reason`, `is_error`,
`is_complete`, `text`, `tool_calls`, `usage`, `notices`, `context_events`, and
`error`.

Exit codes:

```text
0    stop_reason == end_turn
2    agent error, or incomplete turn (max_tokens / max_turns / refusal)
1    config / provider / session error
130  interrupted
```

In headless mode there is no interactive permission asker: use
`--permission-mode bypass` (or allow rules) if the task needs shell commands.

## Usage

```bash
cli-agent                              # interactive REPL
cli-agent -c                           # resume the latest REPL session
cli-agent --resume <session-id>
cli-agent --permission-mode default
cli-agent --provider deepseek           # REPL, defaults to deepseek-flash[1M] + thinking
cli-agent --provider deepseek --no-extended-thinking
                                        # disable the provider's thinking default
cli-agent --no-thinking                 # hide model reasoning in the REPL

cli-agent run "fix the failing test"    # one-shot
cli-agent run -                         # one-shot, prompt from stdin
cli-agent run -c "continue"             # one-shot, resume the latest session
cli-agent run --permission-mode bypass "run the tests"
cli-agent run --output-format jsonl "..."  # JSONL events
cli-agent run --thinking "..."           # reasoning on stderr
```

Common options may be placed before or after `run` (for example
`cli-agent --provider deepseek run "..."` and `cli-agent run --provider deepseek
"..."` are equivalent).

On resume, explicit `--provider`/`--model` override the saved session. If you
change the provider without a model, the new provider's default model is used.

`--extended-thinking` is a request capability: it sends the provider's `thinking`
parameter and requires the selected provider/model to declare `supports_thinking`.
Providers may also default it on (`thinking_by_default`); the DeepSeek preset does,
so `cli-agent --provider deepseek` requests thinking without extra flags.
`--thinking/--no-thinking` only controls display, so `--thinking` alone will not make
the model think. Use a reasoning-capable model; for DeepSeek, `deepseek-flash`
(not `deepseek-chat`) produces reasoning.

Built-in tools: `Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `load_skill`.

Sessions, memory, and session spill files live under
`$XDG_DATA_HOME/cli-agent/projects/<project-id>/`. Project instructions are read from
`AGENTS.md` and `.agents/`; `cli-agent` does not reuse `.claude/` or `CLAUDE.md`.
`@include` paths in project instructions are restricted to the workspace; includes in
the global config `AGENTS.md` are restricted to the config directory.

## Learn

- [01 · 一次流式工具调用：从 SSE 到 ToolResultBlock](learn/01_streaming_tool_call.md)
  —— 模型、API、SSE、SDK、adapter、runtime、ToolExecutor 的完整路径；以及为什么内部
  Block/Delta 不直接使用 SDK 类型。

## System prompt

The prompt is assembled from ordered modules under `src/cli_agent/context/prompts/`:
identity, workspace, tools, file/shell policies, project instructions, memory, skills,
and compaction. The tool list is generated from the live registry, and modules only
claim capabilities the harness actually implements.

## Status (v1)

Implemented today:

- one async agent loop with streaming text, normalized tool calls, retries,
  `max_tokens` continuation, and cancellation;
- explicit provider abstraction plus the Anthropic-compatible adapter
  (Anthropic, DeepSeek, Kimi, MiniMax, GLM presets);
- `Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `load_skill`;
- permission modes, allow/deny rules, workspace boundary, interactive and
  fail-closed non-interactive asks;
- append-only JSONL sessions, `-c/--resume`, transcript repair, context checkpoints;
- ordered context compaction (spill, prune, snip, summarize), reactive compaction;
- file-backed memory with lexical selection, background extraction, and a
  single-flight coordinator;
- skills from `~/.config/cli-agent/skills` and `.agents/skills`;
- extended thinking requests for providers that support them (Anthropic and DeepSeek
  presets; opt in another provider with `supports_thinking = true`), with provider
  defaults (`thinking_by_default`), budget validation, and signed-reasoning replay
  after tool calls. The DeepSeek preset defaults to `deepseek-flash[1M]` with thinking
  on;
- throttled background memory extraction (new-message threshold + cooldown, both
  configurable) with retryable provider errors retried and non-fatal failures logged
  as warnings rather than tracebacks;
- streaming reasoning display: on by default in the REPL, off in one-shot mode;
  `--thinking/--no-thinking` overrides it (one-shot mode writes reasoning to stderr);
- REPL (`cli-agent`), one-shot (`cli-agent run`), and `/help /clear /compact /model /cost /exit`;
- headless `--output-format text|json|jsonl` with a stable result schema, clean
  stdout, stderr diagnostics, and non-zero exit on incomplete turns.

Deliberately deferred: MCP, external hook processes, subagents, teams, worktrees,
cron/background tasks, workflow runtime, goal loop, web tools, LSP, notebook, plan
mode, tool search, OpenAI-compatible adapter, and a public plugin SDK.

Notes: compaction and memory extraction are best-effort; on a clean REPL exit the
runtime waits briefly for in-flight extraction. Permission rules are approval UX,
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
