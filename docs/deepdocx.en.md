# DeepDocx document workspace

[简体中文](deepdocx.md) | **English** · [Home](../README.en.md)

## Start a document session

Use your existing provider configuration in DeepCodocx:

```bash
uv sync --extra dev
uv run dcx web
```

Click **DeepDocx**, connect a document directory, and choose a fresh conversation
or a fork of the current discussion. The directory is on the machine running the
Python server. You can select `deep-docs-unified/data` or its
`data/paper-bundles` directory directly. Select individual documents to narrow the
scope; leaving the selection empty uses the whole library.

You can also start with an existing library directly:

```bash
uv run dcx web --library /path/to/paper-bundles
```

This creates a DeepDocx session immediately. To choose whether to inherit a
general conversation, start the general Web interface first and enter through
the button. The document service consumes preprocessed bundles and reads their
existing text and images.

CLI / headless examples:

```bash
dcx run --library /path/to/paper-bundles "Explain MEMORYVLA's memory mechanism using the original text and figures"
dcx --mode deepdocx -c
dcx run --resume SESSION_ID "Continue with the next section"
dcx run --resume NORMAL_SESSION --fork --library /path/to/paper-bundles "Continue from our earlier discussion"
```

Within a workspace, plain `-c` resumes the latest general session;
`--mode deepdocx -c` resumes the latest document session. An explicit session ID
restores its saved mode. Existing Bash approval rules continue to apply.

## Library configuration

You can configure directories in `~/.config/dcx/config.toml`:

```toml
[deepdocx]
libraries = ["/path/to/deep-docs-unified/data"]
# Optional initial default; it does not also need to appear in libraries.
default_library = "/path/to/deep-docs-unified/data"
```

Directories connected through the UI are stored in
`$XDG_DATA_HOME/dcx/libraries.json`. Successfully entering DeepDocx also
saves the selected library. It is selected again after closing the page,
restarting the server, or creating another session. Registering another
directory preserves the previous selection.

Selection priority is: the current document session's binding, the last
successful selection, `default_library`, then an available configured or saved
library. When none is saved or configured, the app checks for
`deep-docs-unified/data` near the current workspace. A valid bundle library is
added to the selector automatically. In that layout, `dcx web` can offer
the library without manual registration, and `dcx web --mode deepdocx`
can use it directly. If no valid library exists, the dialog explains how to
connect one and disables entry until one is available.

Session mode, library bindings, document scope, and fork boundaries are stored in
append-only session records. Older sessions default to `normal`. Selecting a
library does not change the coding working directory, load the library's
AGENTS.md, or save mode instructions through Remember.

Source bundles and existing SQLite indexes are accessed read-only. An absent
index falls back to lexical scanning; interactive requests do not rebuild the
source index. Incomplete bundles and unsupported schemas are skipped. A session
uses a snapshot of the discovered directory; create a new document session to
pick up newly converted bundles.

## Fixed tool entries and MCP

Both modes expose the same fixed entries:

```json
{"name":"DiscoverTools","input":{"query":"document paper search read view guide","limit":5}}
```

```json
{"name":"CallExternalTool","input":{"tool":"documents.doc_search","arguments":{"query":"memory","top_k":5}}}
```

These examples illustrate the call shape, not a provider's wire format.
Discovery returns tool descriptions and `inputSchema`; the model invokes a
discovered tool through the second entry. Discovery does not change the
provider-facing tool list or system prompt.

Execution validates arguments against the actual tool schema and checks
permissions under its namespaced tool name. External tools not declared
read-only require approval. The generic client handles stdio initialization,
paginated tool lists, calls, server notifications, text/image results, timeouts,
and cancellation cleanup. Server diagnostics go to stderr.

To add your own stdio MCP service:

```toml
[mcp_servers.notes]
command = "python"
args = ["/path/to/notes_server.py"]
read_tools = ["search", "read"]
timeout = 30

[mcp_servers.notes.env]
NOTES_PATH = "/path/to/notes"
```

`read_tools` is a local declaration. A server's `readOnlyHint` does not grant
permissions automatically. Only list tools whose operations are read-only.
The name `documents` is reserved for the selected document library.

## Document MCP tools

| Tool | Purpose | Main arguments |
|---|---|---|
| `documents.doc_library` | List papers or resolve them by title | Optional `query` |
| `documents.doc_search` | Locate text, figures, and tables with keywords | `query`, `doc`, `kind`, `top_k` ≤ 20 |
| `documents.doc_read` | Read a source unit, its neighbors, or a page | `locator`, `neighbors` ≤ 20, `max_chars` ≤ 16000 |
| `documents.doc_view` | Return an actual page or figure image with its source | `locator` |
| `documents.doc_guide` | Load detailed workflow guidance on demand | None |

Native Bash, Read, and Grep can be combined with these tools. The guide explains
both bundle layouts so the agent can inspect existing images without parsing
the PDF again. The document tools return evidence; planning and answering
remain with the main agent.

The document service can also run independently for other MCP clients:

```bash
python -m dcx.deepdocx.server --root /path/to/paper-bundles
```

## Sources and heterogeneous renditions

Example locators:

```text
doc:paper_6d40ee70d189@v1/basic/unit:c_0001_0000
doc:paper_6d40ee70d189@v1/marker-json-v1/unit:u_000037
doc:paper_6d40ee70d189@v1/marker-json-v1/page:3
```

Locators explicitly include the source version and rendition. Units from basic
and Marker are not substituted for one another based on unit ID. Results
include the library, document, version, rendition, page, bbox when available, and
locator.

Answers can use `[source](doc:...)` links to open the source preview. Tool cards
also provide source buttons. The preview shows nearby text and the existing
page image or figure crop. To give the model actual image input, use `doc_view`
or Read.

Basic units are lines; read neighboring units or the whole page for context.
Marker retains paragraph, figure, table, equation, and other unit types. The
existing basic index uses FTS5; Marker uses lexical matching. Original captions
may be missing, and figures or equations may need visual inspection to verify
details. If no crop is available, the service uses the containing page.

These locators belong to this project's document MCP interface. When connecting
another service, use its actual returned schema.

## Session and interface behavior

- DeepDocx and general sessions have separate history lists. Returning to Agent
  opens the latest general session.
- A fork preserves the source session and copies its history and resources.
  Code rewind cannot cross the new session's fork boundary.
- Forks share the coding working directory; no Git worktree is created.
  Inherited history is read-only in the interface.
- Library bindings and document mode remain available after new-session,
  resume, compact, and clear operations.
- Mode guidance is saved separately as `host_context` on messages and rendered
  into provider input with a host-source marker. The chat displays the user's
  original input. A later turn does not remove the guidance from earlier
  request prefixes.
- Switching libraries or scope creates a new DeepDocx session, with an option
  to fork the current discussion.
- Source previews open on demand. Narrow screens use an overlay; wide screens
  use three columns. Theme and size preferences stay in the browser.

## Current limitations

Vector search, image similarity retrieval, generated captions, semantic block
indexes, PDF upload/ingestion, remote MCP transports, and multi-user operation
are not implemented in this version. For an uploaded image, a vision-capable
model can extract visible terms and try keyword lookup. Images without text
need other available clues or future vector tools. The agent should not claim
to have performed vector search.

Image analysis requires actual image support from the model and endpoint.
Reading metadata or showing an image in the browser does not mean the model
received it. Network search likewise requires a real available external tool.

See the [development guide](development.en.md) for environment setup and browser
acceptance checks.
