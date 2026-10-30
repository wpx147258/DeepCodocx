# Configuration guide

[简体中文](configuration.md) | **English** · [Home](../README.en.md)

Configuration is read from `$XDG_CONFIG_HOME/dcx/config.toml`, normally
`~/.config/dcx/config.toml`. Use `--config /path/to/config.toml` to select
another file. See [config.example.toml](../config.example.toml) for a full example.

Runtime data uses `$XDG_DATA_HOME/dcx`, normally `~/.local/share/dcx`. On upgrade,
if a `dcx` directory is absent and the corresponding `cli-agent` directory exists,
a symlink exposes the existing configuration, skills, memory, sessions, and saved
libraries at the new path. An existing independent `dcx` directory takes
precedence. See the [upgrade notes](troubleshooting.en.md).

## Providers

Provider selection is explicit. `--provider` / `--model` override configuration.
The current adapter uses the Anthropic-compatible protocol, with these presets:

| Provider | API key environment variables |
|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` |
| `deepseek` | `DEEPSEEK_API_KEY` |
| `kimi` | `MOONSHOT_API_KEY` |
| `minimax` | `MINIMAX_API_KEY` |
| `glm` | `GLM_API_KEY` or `ZHIPU_API_KEY` |

Minimal configuration:

```toml
default_provider = "deepseek"
permission_mode = "accept-edits"
```

Declare capabilities explicitly when adding a compatible endpoint. Use a model
and token limits that the server actually supports:

```toml
[providers.my_endpoint]
kind = "anthropic_compat"
base_url = "https://example.com/anthropic"
api_key_env = ["MY_API_KEY"]
default_model = "your-model"
context_window = 128000
max_output_tokens = 8192
supports_images = false
supports_thinking = false
```

`<PROVIDER>_BASE_URL` can override the endpoint; Anthropic also supports
`ANTHROPIC_BASE_URL`. Model-specific overrides belong under
`[models."your-model"]`. Image input requires support from both the model and
the endpoint.

## Thinking and caching

`--extended-thinking` controls whether extended thinking is requested from the
provider and requires `supports_thinking` in its profile. `--thinking` controls
display. One-shot mode writes displayed reasoning to stderr.

```toml
[thinking]
# enabled = false
budget_tokens = 4096
```

The budget must be at least 1024 and smaller than the maximum output token
limit. When `enabled` is unset, the provider default applies; the DeepSeek preset
enables thinking by default. Providers that require signed reasoning receive the
necessary reasoning blocks again when a tool cycle continues.

`prompt_caching = true` adds cache breakpoints to the static system prefix and
tool schemas. The Anthropic preset enables this. Other compatible endpoints
default to off; confirm that the endpoint accepts the parameter before enabling
it.

## Document libraries

```toml
[deepdocx]
libraries = ["/path/to/paper-bundles"]
default_library = "/path/to/paper-bundles"
```

Libraries registered in the UI and the last selection are stored in
`$XDG_DATA_HOME/dcx/libraries.json`. An active document session uses its own
binding; otherwise, the last selection, configured default, or available library
is used. When nothing is configured or saved, a nearby `deep-docs-unified/data`
can be discovered. See the [DeepDocx guide](deepdocx.en.md) for input formats and
document scopes.

## MCP

The generic MCP client supports local stdio. Executable commands come from local
configuration; individual schemas are returned through discovery on demand.

```toml
[mcp_servers.notes]
command = "python"
args = ["/path/to/notes_server.py"]
read_tools = ["search", "read"]
timeout = 30

[mcp_servers.notes.env]
NOTES_PATH = "/path/to/notes"
```

Only the local `read_tools` list grants a read-only classification. Other tools
enter permission checks under their actual names and arguments. Server
annotations do not grant permissions automatically. The `documents` namespace is
reserved for the selected document library.

## Permissions and memory

```toml
[[permissions.deny]]
tool = "Bash"
args = { command = "*rm -rf*" }

[memory]
enabled = true
```

Rules use glob matching on tool names and arguments, with deny taking precedence.
Shell rules are not a shell parser or an isolation boundary. See the
[usage guide](usage.en.md) for modes and approval behavior.

The model writes memory through `Remember` for durable user preferences and
constraints. Mode guidance and task state that can be read again do not depend
on memory. Use `--no-memory` to disable long-term memory.

Multimodal indexes have independent Gitee embedding/caption and DashScope native
fusion configuration. See the [index guide](multimodal-index.en.md).
