# DeepDocx document workspace

[简体中文](deepdocx.md) | **English** · [Home](../README.en.md)

## Start a document session

Use your existing provider configuration in DeepCodocx:

```bash
uv sync
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

Document tools access source bundles and existing full-text catalogs read-only.
An absent catalog falls back to lexical scanning; interactive requests do not
rebuild it. Incomplete bundles and unsupported schemas are skipped. Web/CLI
ingestion publishes a library revision, allowing Web readers and running MCP
processes to refresh on their next read. A restricted session keeps its document
scope; start a new session or fork to include documents outside it. For bundles
copied externally, a new document session rediscovers the files, while the
semantic index needs a manual sync.

## Multimodal semantic lookup

Index an existing library without parsing its PDFs again. Index management groups
the representations into four families:

| Index family | Contents | Typical use |
|---|---|---|
| Text blocks | Marker paragraphs/lists, or Basic lines grouped by layout | Locate passages and explain concepts |
| Images | Figure/table crops; page images for Basic-only sources | Image queries or finding diagrams by text |
| Captions and descriptions | Original captions and generated visual descriptions, stored separately | Locate figures by caption or described content |
| Native image–text combinations | Image + original caption, image + generated description | Combine visual and textual clues |

Set `GITEE_API_KEY` and `DASHSCOPE_API_KEY` in the terminal launching Web, enter
DeepDocx, and open **索引管理** (Index management) beside the library. Choose
which papers to synchronize using the searchable selection, then choose whether
to generate descriptions and select their model. Full-library synchronization
is an explicit option; a single-paper job preserves other indexed papers.
The first manual sync enables synchronization after future ingestion for that
library. Credentials alone do not trigger a full build. Indexing and description
generation run in independent background jobs without adding chat messages;
you can close the panel and continue the conversation.

Use **试用语义检索** (Try semantic search) for text, image, or mixed queries and
open a result's source text/image. This preview does not create an agent turn.
In conversation, the agent discovers semantic tools on demand and continues to
combine keyword lookup, Bash, Read, and image reading itself.

DashScope `qwen3-vl-embedding` handles both independent and native joint encoding
at 2560 dimensions by default. Gitee provides descriptions and an explicit backup
standalone encoder configuration. Query channels are encoded separately and
rankings are fused. Missing or stale indexes produce explicit guidance;
the agent can use keyword lookup without starting an index build during a turn.
See [multimodal indexes](multimodal-index.en.md) for configuration, synchronization,
and caption provenance.

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
| `documents.doc_index_status` | Inspect semantic coverage and availability for the library/session scope | None |
| `documents.doc_semantic_search` | Locate evidence with text, an image, or both | `query`, `image_ref`, `target=auto/text/image`, `doc`, `top_k` (default 5, maximum 20) |

Native Bash, Read, and Grep can be combined with these tools. The guide explains
both bundle layouts so the agent can inspect existing images without parsing
the PDF again. The document tools return evidence; planning and answering
remain with the main agent.

Semantic tools are connected through host configuration; use the actual list
returned by `DiscoverTools`. For example, discover
`document semantic search index status`, then call:

```json
{"name":"CallExternalTool","input":{"tool":"documents.doc_semantic_search","arguments":{"query":"How does memory connect to action prediction?","target":"text","top_k":5}}}
```

Provide query text, an image, or both. Chat uploads receive a stable
`attachment:...` reference in host message context; pass it as `image_ref`.
A library image's exact `doc:...` locator also works. `target=auto` searches
text/caption evidence for text queries and image/native-joint evidence when an
image is supplied. `text` selects source text blocks; `image` selects image-related
representations. Task planning stays with the agent.

The default result contains at most five compact source cards and `coverage`,
including indexed paper names. Unindexed papers are not treated as absent
evidence. No image pixels or full paragraphs are attached: inspect originals
through `doc_read`/`doc_view`. `snippet_source` and provenance distinguish generated
descriptions. Legacy `image_locator`/`image_path` and advanced `representations`
remain supported. Image input forms are mutually exclusive; explicit text/image
targets cannot combine with advanced representation filtering. Local paths are
bounded to the workspace, selected library, or current session, with no remote URLs.

Files live under the current session's `attachments/`; resume and fork preserve
references without rewriting original messages/history. The sidebar's semantic
coverage badge opens index management. Tool cards show excerpts and source
buttons, with raw JSON available on expansion.

The document service can also run independently for other MCP clients:

```bash
python -m dcx.deepdocx.server --root /path/to/paper-bundles
```

This standalone command exposes the basic document tools. The dcx host also
passes credential-free semantic settings; see the
[index guide](multimodal-index.en.md) for other clients.

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

Semantic blocks preserve these original units: each block retains its source
locators, title, headings, and bounding box. Caption/joint hits still resolve to
the original image and text, with generation model and input provenance recorded
separately.

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

Optional text-block/image/caption/native-joint indexes support semantic lookup and
image queries. Configure Gitee and DashScope services and inspect coverage in
Index management; see the [index guide](multimodal-index.en.md). Generated
captions are derived descriptions, not source quotations. Missing or stale
indexes remain explicit; keyword and native file tools continue to work.
PDF ingestion runs as an independent system job, described in the
[ingestion guide](deepdocx-ingest.en.md). Remote MCP and multi-user operation
remain future work.

Image analysis requires actual image support from the model and endpoint.
Reading metadata or showing an image in the browser does not mean the model
received it. Network search likewise requires a real available external tool.

See the [development guide](development.en.md) for environment setup and browser
acceptance checks.
