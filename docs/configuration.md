# 配置指南

**简体中文** | [English](configuration.en.md) · [首页](../README.md)

默认读取 `$XDG_CONFIG_HOME/dcx/config.toml`，通常是
`~/.config/dcx/config.toml`。也可用 `--config /path/to/config.toml` 指定文件。
完整示例见 [config.example.toml](../config.example.toml)。

运行数据默认位于 `$XDG_DATA_HOME/dcx`，通常是 `~/.local/share/dcx`。升级时，若对应
`dcx` 目录不存在而旧 `cli-agent` 目录存在，会自动创建指向旧目录的符号链接，继续使用
原有配置、技能、记忆、会话及文档库选择。已有独立 `dcx` 目录优先；详见[升级说明](troubleshooting.md)。

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

界面注册的目录与最后选择保存在 `$XDG_DATA_HOME/dcx/libraries.json`。
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

多模态索引使用独立的 Gitee embedding/caption 与百炼原生融合配置，详见[索引配置](multimodal-index.md)。
