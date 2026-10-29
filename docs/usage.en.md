# CLI and Web guide

[简体中文](usage.md) | **English** · [Home](../README.en.md)

## Three entry points

```bash
uv run dcx --provider deepseek                    # Interactive terminal
uv run dcx web --provider deepseek                # Local Web interface
uv run dcx run --provider deepseek "Describe this repository"  # One shot
```

Common options can appear before or after the subcommand. Run `dcx --help`,
`dcx web --help`, or `dcx run --help` for all options. Provider setup
is covered in the [configuration guide](configuration.en.md).

## Sessions and continuing work

```bash
dcx -c                         # Latest general session
dcx --resume SESSION_ID
dcx --resume SESSION_ID --fork
dcx run -c "Continue the remaining work"
dcx --mode deepdocx -c          # Latest document session
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
`X-DCX-Token`. The legacy `X-CLI-Agent-Token` header remains accepted; when the
new header is present, it takes precedence. Pair network exposure with an
authenticated proxy.

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
dcx run --permission-mode bypass "Run the tests and summarize the results"
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
printf 'Summarize this repository' | dcx run --output-format jsonl -
dcx run --output-format json "Describe the module structure"
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
