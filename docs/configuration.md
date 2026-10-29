<!-- Generated language panels. Edit this file's default-open language, then run python scripts/sync_doc_languages.py. -->

<details name="deepcodocx-doc-language" lang="en">
<summary>English</summary>

<!-- doc-language:en:start -->
# Configuration guide

[Home](../README.en.md)

Configuration is read from `$XDG_CONFIG_HOME/cli-agent/config.toml`, normally
`~/.config/cli-agent/config.toml`. Use `--config /path/to/config.toml` to select
another file. See [config.example.toml](../config.example.toml) for a full example.

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
`$XDG_DATA_HOME/cli-agent/libraries.json`. An active document session uses its own
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
<!-- doc-language:en:end -->

</details>

<details name="deepcodocx-doc-language" lang="zh-CN" open>
<summary>简体中文</summary>

<!-- doc-language:zh-CN:start -->
# 配置指南

[首页](../README.md)

默认读取 `$XDG_CONFIG_HOME/cli-agent/config.toml`，通常是
`~/.config/cli-agent/config.toml`。也可用 `--config /path/to/config.toml` 指定文件。
完整示例见 [config.example.toml](../config.example.toml)。

## Provider

Provider 必须显式选择；命令行可用 `--provider` / `--model` 覆盖配置。
当前适配器使用 Anthropic-compatible 协议，提供以下预设：

| Provider | API key 环境变量 |
|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN` |
| `deepseek` | `DEEPSEEK_API_KEY` |
| `kimi` | `MOONSHOT_API_KEY` |
| `minimax` | `MINIMAX_API_KEY` |
| `glm` | `GLM_API_KEY` 或 `ZHIPU_API_KEY` |

最小配置：

```toml
default_provider = "deepseek"
permission_mode = "accept-edits"
```

新增兼容端点时显式声明能力，并采用服务端实际支持的模型与窗口：

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

`<PROVIDER>_BASE_URL` 可以覆盖地址，Anthropic 也支持 `ANTHROPIC_BASE_URL`。
按模型声明的覆盖放在 `[models."your-model"]` 中。视觉支持需要模型与端点同时可用。

## 思考与缓存

`--extended-thinking` 控制是否向 provider 请求扩展思考，要求 profile 声明
`supports_thinking`。`--thinking` 控制显示；一次性模式将显示内容写到 stderr。

```toml
[thinking]
# enabled = false
budget_tokens = 4096
```

预算至少为 1024，并且小于最大输出 token。未设置 enabled 时采用 provider 默认值；
DeepSeek 预设默认开启。支持签名推理的会话会在工具循环中重放必要的 reasoning blocks。

`prompt_caching = true` 会给静态 system 前缀和工具 schema 添加缓存断点。Anthropic
预设启用，其他兼容端点默认关闭；应先确认端点接受该参数。

## 文档库

```toml
[deepdocx]
libraries = ["/path/to/paper-bundles"]
default_library = "/path/to/paper-bundles"
```

界面注册的目录与最后选择保存在 `$XDG_DATA_HOME/cli-agent/libraries.json`。
有当前文档会话时优先使用其绑定，否则采用上次选择、配置默认值或已有库。
没有配置或保存的库时，会发现附近的 `deep-docs-unified/data`。
更详细的格式和文档范围说明见 [DeepDocx 指南](deepdocx.md)。

## MCP

通用 MCP 客户端支持本地 stdio。命令来自配置，具体 schema 通过发现工具按需返回。

```toml
[mcp_servers.notes]
command = "python"
args = ["/path/to/notes_server.py"]
read_tools = ["search", "read"]
timeout = 30

[mcp_servers.notes.env]
NOTES_PATH = "/path/to/notes"
```

只有本地 `read_tools` 声明会赋予只读分类；其他工具按实际名称和参数进入权限检查。
MCP 服务端 annotations 不自动授予权限。`documents` 命名空间由所选文档库使用。

## 权限与记忆

```toml
[[permissions.deny]]
tool = "Bash"
args = { command = "*rm -rf*" }

[memory]
enabled = true
```

规则按工具名与参数 glob 匹配，deny 优先。Shell 规则不是语法解析器，不能作为隔离
边界。模式和审批细节见[使用指南](usage.md)。

记忆由模型通过 `Remember` 写入，用于持久用户偏好和约束。模式资料和可重读的任务
状态不依靠记忆保存；`--no-memory` 可禁用长期记忆。
<!-- doc-language:zh-CN:end -->

</details>
