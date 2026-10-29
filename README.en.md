# DeepCodocx

[简体中文](README.md) | **English**

**One agent workspace for code, papers, and figures.**

[![CI](https://github.com/wpx147258/DeepCodocx/actions/workflows/ci.yml/badge.svg)](https://github.com/wpx147258/DeepCodocx/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

DeepCodocx helps an agent work across code repositories and document libraries:
run commands, edit files, locate passages in papers, inspect figures, and bring
the supporting evidence back to you. The terminal, browser, and automation
interfaces share one agent runtime.

In **DeepDocx document mode**, connect a preprocessed library to discuss paper
concepts, excerpts, and figures. Progressive context and MCP tools discovered on
demand give the agent access to documents. The agent decides how to combine
search, reading, visual inspection, and native Bash / Read tools.

![DeepDocx: conversation, document navigation, and source figure preview](docs/web/deepdocx-light.png)

[Dark theme](docs/web/deepdocx-dark.png) · [Mobile](docs/web/deepdocx-mobile.png) ·
[General agent workspace](docs/web/chat-light.png)

## Why try it?

- **Follow an answer back to its evidence.** Click a source to open its text,
  page, or figure. Locations retain the document version, rendition, and page.
- **Combine code and document tools freely.** Built-in file and shell tools work
  alongside local MCP capabilities discovered on demand. The same agent decides
  which evidence it needs and what to do next.
- **Keep a discussion moving.** Save and resume sessions, fork a discussion, and
  compact long conversations. Preview Write / Edit file changes and choose how
  to restore them before editing and resending a message.
- **See what is happening.** A lightweight Web interface brings together
  streaming replies, tool inputs and outputs, approval prompts, usage, and
  context readouts. Terminal interaction and JSON / JSONL interfaces are also
  available.

## Get started in minutes

You need Python **3.12+** and [uv](https://docs.astral.sh/uv/). For DeepSeek:

```bash
git clone https://github.com/wpx147258/DeepCodocx.git
cd DeepCodocx
uv sync

export DEEPSEEK_API_KEY="your-api-key"
uv run cli-agent web --provider deepseek
```

Open the address printed in your terminal, normally `http://127.0.0.1:8765/`.
On WSL or a machine without a graphical browser, add `--no-open`. If the port is
occupied, add `--port 8766`.

The executable is currently named `cli-agent`; the Python package is `dcx`.
Built-in profiles cover Anthropic-compatible endpoints from Anthropic, DeepSeek,
Kimi, MiniMax, and GLM. See the [configuration guide](docs/configuration.en.md)
for more options.

## Bring your document library

Click **DeepDocx**, choose a fresh session or fork the current discussion, and
connect an existing basic / Marker bundle directory. You can limit the scope to
selected papers, and the library selection is remembered across restarts.

You can also start directly in document mode:

```bash
uv run cli-agent web --provider deepseek --library /path/to/paper-bundles
```

Locate a paper, read the relevant passages, inspect the original figures, and
explain the method with evidence, all within one discussion. An available
library is selected automatically. The [DeepDocx guide](docs/deepdocx.en.md)
describes accepted inputs and document workflows.

The interface examples use preprocessed papers and a scripted provider. Actual
answer quality depends on the selected model and the source documents.

## Prefer the terminal or scripts?

```bash
# Interactive coding agent
uv run cli-agent --provider deepseek

# Resume the latest general session
uv run cli-agent -c

# Locate and explain a concept in a document library
uv run cli-agent run --provider deepseek --library /path/to/paper-bundles \
  "Explain the paper's memory mechanism using the original text and figures"

# Stream events for automation
printf 'Describe this repository: entry points and main modules' | \
  uv run cli-agent run --provider deepseek --output-format jsonl -
```

## Documentation

| Guide | Covers |
|---|---|
| [DeepDocx document guide](docs/deepdocx.en.md) | Libraries, figures, citations, and document MCP tools |
| [CLI and Web guide](docs/usage.en.md) | Sessions, permissions, rewind, terminal commands, and structured output |
| [Configuration](docs/configuration.en.md) | Providers, thinking, libraries, and external MCP servers |
| [Architecture](docs/architecture.en.md) | The shared runtime, tool boundaries, context, and persistence |
| [Development](docs/development.en.md) | Development setup, testing, and contribution conventions |
| [Troubleshooting](docs/troubleshooting.en.md) | WSL, ports, project renames, and session history |

## Current scope

Code tools, persistent sessions, keyword document search, text and image
reading, and local stdio MCP are available today. Image and semantic vector
search, generated captions, PDF upload and ingestion, and remote MCP transports
remain future work.

The permission system provides approvals and operation controls. Use external
isolation for untrusted tasks. The Web server binds to loopback by default;
non-loopback access requires an API token. See the [usage guide](docs/usage.en.md)
for details.

[MIT License](LICENSE)
