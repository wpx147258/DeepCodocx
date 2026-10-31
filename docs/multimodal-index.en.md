# Multimodal indexes and semantic lookup

[简体中文](multimodal-index.md) | **English** · [Home](../README.en.md)

DeepDocx indexes semantic text blocks, images, captions/descriptions, and native
image–caption combinations. Retrieval returns source locations and short evidence
cards. The existing agent decides how to read, inspect, and answer; keyword
search, Bash, Read, and the single agent loop remain available.

## Models and spaces

| Capability | Default service and model | Representation |
|---|---|---|
| Text blocks, images, original/generated caption vectors | DashScope `qwen3-vl-embedding`, `enable_fusion=false` | Independent encoding; 2560 dimensions |
| Image + original caption, image + generated description | DashScope `qwen3-vl-embedding`, `enable_fusion=true` | Native joint encoding; 2560 dimensions |
| Image descriptions | Gitee AI `qwen3.8-flash` or `DeepSeek-V4.1-Flash-1017` | Generated text and provenance, subsequently embedded by DashScope |
| Backup standalone encoder | Gitee AI `Qwen3-VL-Embedding-8B`, explicitly configured | Separate Gitee space; configurable as 1024 dimensions |

Both the DashScope service default and the project default are **2560** dimensions.
Supported alternatives are 2048, 1536, 1024, 768, 512, and 256. Requests explicitly
set the dimension and `enable_fusion`. Independent requests batch up to 10 entries;
joint requests send the image and its caption together. Text, image, and mixed
queries use the corresponding encoders.

All vectors use the same hosted DashScope model by default. Independent and joint
query channels remain separate in the implementation; RRF combines their rankings.
`score` is a rank score, while `similarities` retains within-channel cosine values.
Averaged vectors never substitute for native fusion. Switching to Gitee or another
joint deployment isolates results by configuration profile and requires syncing
the selected documents. Service failure never silently inserts another model's
vectors into an existing index.

The Gitee backup gateway supports standalone text/image inputs but not native
image–text fusion. Joint encoding can remain on DashScope. Equal dimensions do
not make different services share a vector space.

### Four families, six representations

The interface groups indexes into four functional families. Original captions
and generated descriptions remain distinct, giving six `representation` values
for storage and filtering rather than four unrelated databases:

| UI family | Representation | Input and provenance |
|---|---|---|
| Text blocks | `text_block` | Title, headings, and block text; all source unit locators retained |
| Images | `image` | Figure/table crops or explicitly labeled page images |
| Captions/descriptions | `caption_original` | Original caption extracted from the bundle |
| Captions/descriptions | `caption_generated` | Visual model description with independent provenance |
| Native joint | `image_caption_original` | Image and original caption in one native joint input |
| Native joint | `image_caption_generated` | Image and generated description in one native joint input |

By default, the first four use DashScope independent encoding; the final two use its native joint encoding.
All are stored in one SQLite derivative database per library. Missing original
captions do not create invented original-caption records. Disabling descriptions
omits generated-caption and corresponding joint representations. Basic page
images can be indexed immediately; sync again after adding Marker artifacts.

## Getting started

Set credentials in the terminal that launches the Web server:

```bash
export GITEE_API_KEY="your-gitee-key"
export DASHSCOPE_API_KEY="your-dashscope-key"
uv sync
uv run dcx web --provider deepseek
```

Keys are read only by server processes. They are excluded from browser payloads,
job JSON, bundles, and agent prompts. Restart the host after changing its shell
environment. The default DashScope endpoint is Beijing; configure the matching
endpoint for credentials in another region.

Enter DeepDocx and select **索引管理** (Index management) beside the library:

1. Inspect complete/partial coverage and the four representation counts.
2. Choose whether to generate image descriptions and select the caption model.
3. The default **选择论文** (Select papers) lets you search and select one or several papers, then synchronize. Select **整个文档库** (Entire library) explicitly for a full build. The first manual sync also enrolls future ingestions in automatic synchronization.
4. Close the panel while work continues; persisted jobs survive host restarts.
5. Expand **试用语义检索** (Try semantic search), provide text, an image, or both,
   and open a result in the existing evidence panel.

![Index management](web/deepdocx-index-light.png)

[Dark theme](web/deepdocx-index-dark.png) · [Mobile](web/deepdocx-index-mobile.png)

**重新生成** (Regenerate) bypasses inference caches for the selected documents.
Ordinary synchronization reuses unchanged results. Hosted services may charge for
requests; initial builds and regeneration scale with text-block and image counts.
Page images are explicitly labeled as pages rather than extracted figures.

Index management supports selected papers and shows the affected count. No selection
disables synchronization. A scoped sync/regeneration preserves other indexed papers.
Query previews and agent retrieval still respect the current session's document scope.
Reopening the panel returns to paper selection, using the session scope or the
most recent scoped job. Buttons explicitly show the affected count, such as
“同步 1 篇” (Sync 1 paper) or “同步全库 76 篇” (Sync all 76 papers). Description/model
choices persist per library and apply to later syncs and ingestion-triggered jobs.
Closing the panel or stopping a conversation does not cancel indexing; use the
panel's stop action or CLI `index cancel`.

## Configuration

```toml
[deepdocx.index]
protocol = "dashscope"
enabled = true
auto_sync = false # First manual sync enables automatic sync for that library.
base_url = "https://dashscope.aliyuncs.com/api/v1"
api_key_env = "DASHSCOPE_API_KEY"
model = "qwen3-vl-embedding"
dimensions = 2560
batch_size = 8
timeout = 60
block_chars = 1800
joint_protocol = "dashscope"
joint_base_url = "https://dashscope.aliyuncs.com/api/v1"
joint_key_env = "DASHSCOPE_API_KEY"
joint_model = "qwen3-vl-embedding"
joint_dimensions = 2560
revision = "qwen3-vl-v1"

[deepdocx.caption]
enabled = true
base_url = "https://ai.gitee.com/v1"
api_key_env = "GITEE_API_KEY"
model = "qwen3.8-flash" # Or DeepSeek-V4.1-Flash-1017; selectable per library.
max_tokens = 800
timeout = 90
revision = "scientific-description-v1"
```

DashScope independent and joint encoders support dimensions 256, 512, 768, 1024, 1536, 2048, or 2560; the two
spaces may have different dimensions. Relevant model, preprocessing, chunking,
and prompt revision changes invalidate the previous index generation. “Configured”
service status checks configuration and credential presence; inference failures
are reported in job outcomes.

`enabled=true` permits index services; it does not mean an index has been built.
Global `auto_sync` is the default only when no per-library preference exists;
manual synchronization saves `auto_sync=true` for that library. Saved description
and model choices likewise override TOML defaults. Changing the joint endpoint
can reuse unchanged standalone embedding caches, but a sync is needed to publish
document generations matching the new configuration.

## Bundle coupling and synchronization

Bundles hold source evidence; vectors are rebuildable derivatives. **Vectors,
generated captions, and jobs do not modify bundles**, manifests, checksums, or
historical citations. Existing bundles can be indexed without reparsing PDFs.

```text
$XDG_DATA_HOME/dcx/semantic/<library-id>/
  index.sqlite
  index.sqlite-wal
  jobs/<job-id>/settings.json  # Configuration snapshot, no keys.
  jobs/<job-id>/worker.log
```

The library ID follows its normalized root path. Moving a library creates a new
identity and requires a new sync. Records retain document ID, version, rendition,
unit/asset identity, exact source locator, and input fingerprint.

| Event | Behavior |
|---|---|
| Initial sync | Enumerate complete bundles, build semantic/image samples and vector representations |
| Unchanged content | Skip the document without inference requests |
| New Web/CLI ingestion | Enqueue only documents published by this ingestion task for enrolled libraries; do not build all pending old papers |
| Ingestion during synchronization | Request another incremental pass without competing writers |
| Bundle replacement or edit | Exclude stale vectors on the next query; atomically replace document records during sync |
| External bundle copies/deletions | Exclude disappeared/stale sources; manual sync adds new records and prunes removed documents |
| Model/caption changes | Avoid mixing generations; reuse still-applicable cached inputs |
| Partial service failure | Keep available representations; mark partial and retry missing work on sync |
| Cancellation | Stop subsequent work, retaining committed documents and reusable inference caches |

File hashes are cached against size, mtime, and ctime. Queries still verify the
current library, document scope, and fingerprints. A document's vector generation
commits in one SQLite transaction. Embedding caches use input and configuration
identity; caption caches preserve their independent provenance.

The first backend is SQLite with sqlite-vec's native cosine distance function for
exact search. It needs no separate database daemon and targets local document
libraries. A larger corpus may need an ANN backend and further performance
measurement; ANN is not implemented here.

### Reading status

| Status | Meaning and action |
|---|---|
| `ready` | Source fingerprint and configuration match; required representations are complete |
| `partial` | A current generation has usable representations but some requests failed; ordinary sync retries missing work |
| `pending` | No completed generation matches current input/configuration; synchronize |
| `stale_documents` | Stored documents no longer match current sources/configuration or valid scope; excluded from retrieval |
| `source_errors` | Bundle/asset reading failed; repair sources and sync, while healthy documents remain processable |

`partial_documents` is also included in `pending_documents`; do not add all
counters as disjoint totals. Representation counts include only current valid
generations. Job `succeeded` means this pass reported no errors; inspect errors
for `partial`/`failed`. `cancelled` retains prior commits. After an unexpected
worker exit, synchronize again to reuse completed inference caches without
replaying any agent conversation.

## Samples and provenance

Marker paragraphs and lists are preferred per source version. Short neighboring
units can merge within the same section/page; long units are bounded. Basic lines
are grouped by page geometry, columns, and paragraph gaps. This is a traceable
heuristic, not complete layout understanding. New Basic conversion separates
words across clear column gutters. An old bundle that already interleaved columns
cannot recover discarded word geometry during indexing; prefer Marker paragraphs
for higher layout fidelity. Each block retains all original
locators, headings, title, and bounding box.

Marker figure/table crops are preferred. Basic-only documents use explicit
`image_kind=page` samples. Caption representations remain distinct:

- `caption_original`: the extracted original figure caption.
- `caption_generated`: the visual model description, recording model, revision,
  timestamp, input hash, and associated source context.
- `image_caption_original` / `image_caption_generated`: native fused inputs for
  the image and the corresponding caption source.

Generated descriptions may use original captions and same-page context, but must
not be quoted as the paper's text. Changing caption models does not overwrite the
original. Source previews still open the original image/text. Indexing sends
bounded, proportionally resized images to configured services, not whole PDFs.

## CLI and read-only MCP

```bash
dcx docs index status --library /path/to/paper-bundles
dcx docs index sync --library /path/to/paper-bundles
# Single-paper trial; repeat --doc for several papers.
dcx docs index sync --library /path/to/paper-bundles --doc paper_37126553647c
dcx docs index sync --library /path/to/paper-bundles --no-captions
dcx docs index sync --library /path/to/paper-bundles --no-wait
dcx docs index cancel --library /path/to/paper-bundles JOB_ID
dcx docs index search --library /path/to/paper-bundles "memory and action"
dcx docs index search --library /path/to/paper-bundles --image figure.png
dcx docs index search --library /path/to/paper-bundles "memory" --image figure.png
dcx docs index search --library /path/to/paper-bundles "memory" --representation text_block
```

Administration commands need no conversational provider or agent session. CLI sync
without `--doc` processes the whole library; repeat `--doc` to limit the documents.
The UI defaults to paper selection to avoid an accidental full-library build. Waiting
sync returns 0 on success and 1 for partial/failure/cancellation. `--force` regenerates
outputs; `--config` selects a configuration file.

Agents discover `documents.doc_index_status` and `documents.doc_semantic_search`
through the existing discovery/call tools. Semantic queries accept text, an image
locator in the selected library, or an image path under the workspace/session
roots, and can filter documents, representations, and result count. They return
bounded candidates, not answers. Model tools cannot build/delete indexes or start
caption batches. Full system prompts stay independent of the index mode;
`doc_search` remains available for keyword lookup.

### Results and source inspection

Queries accept text, an image, or both. `doc_semantic_search` permits up to 12000
characters in `query`; MCP `top_k` is 1–20, default 5. `target=auto` searches
text/caption evidence for text, and image/native-joint evidence for image inputs.
`text` selects text blocks; `image` selects image-related representations.
`image_ref` accepts a host `attachment:...` reference or exact library `doc:...`
image locator, optionally combined with `query`. Legacy `image_locator`/`image_path`
remain mutually exclusive with each other and `image_ref`. Advanced `representations`
cannot combine with explicit text/image targets. `doc` narrows selection within
the session scope. Remote image URLs are rejected.
Web preview accepts one uploaded image up to 5 MiB without adding chat history.

| Field | Meaning |
|---|---|
| `hits` | Bounded candidates with `locator`, `sources`, `doc_id`, version, rendition, page, and bbox when present |
| `snippet` | Up to 500 characters for orientation; use `doc_read` for source text |
| `matched_representations` | Representations supporting this candidate |
| `score` / `similarities` | RRF rank score / within-space cosine values; neither is answer confidence |
| `caption_provenance` | Original/generated caption provenance and applicable generation metadata when present on the hit |
| `snippet_source` | Source text, original caption, or generated description |
| `coverage` | Total/searchable counts and up to 20 indexed paper names/states, with an explicit truncation flag |
| `query_mode` | MCP text, image, or image–text query input |
| `warnings` | Missing current indexes, incomplete coverage, or partial query-service failures |

Candidates merge by source sample. No valid generation returns empty `hits` with
guidance. If one query service fails, other spaces can still return candidates
with warnings. Lexical results are never silently relabeled as vector matches;
the agent can call `doc_search` separately. Source previews show original text
and images. Semantic similarity and generated descriptions require verification;
retrieving an image does not mean the conversational model has inspected it.

Cards contain bounded excerpts and locators, without full paragraphs or image
pixels. Continue through `doc_read`/`doc_view` when needed; status lookup is not a
mandatory preflight. Chat attachments use content hashes under the current
session's `attachments/`. Host request context exposes references without rewriting
original messages. Resume and fork keep them resolvable; query images are not
ingested. The agent decides when to retrieve, read, or use Bash/Read; coverage
badges and cards create no extra turns.

### Standalone MCP clients

dcx passes semantic configuration through `assembly.py`. Other clients running
only `python -m dcx.deepdocx.server --root ...` get basic document tools. To
enable semantic tools, prepare JSON settings and pass `--semantic-settings`.
Fields use the TOML names above, with defaults for omitted settings:

```json
{
  "data_dir": "/absolute/path/to/dcx-data",
  "index": {"enabled": true},
  "caption": {"enabled": true}
}
```

```bash
python -m dcx.deepdocx.server --root /path/to/paper-bundles \
  --semantic-settings /path/to/semantic-settings.json --workspace /path/to/workspace
```

Use the same `data_dir` as the administrative build. The process inherits service
keys from its environment; settings contain configuration and variable names,
not key values. `--workspace` bounds local query-image access. Repeat
`--doc DOC_ID` to restrict document scope. Standalone MCP exposes status and
retrieval only, without starting index builds. For chat attachments, also pass
`--session-root /path/to/current-session`, whose `attachments/` contains reference
files. Only that session is bound; `data_dir` does not grant access to other sessions.

## Gitee backup for independent encoding

Switch the following fields only when using the backup service. Caption generation
retains its separate `[deepdocx.caption]` configuration:

```toml
[deepdocx.index]
protocol = "gitee"
base_url = "https://ai.gitee.com/v1"
api_key_env = "GITEE_API_KEY"
model = "Qwen3-VL-Embedding-8B"
dimensions = 1024
```

Joint encoding can retain the default DashScope 2560-dimensional configuration.
Restart the host and sync selected papers to publish the new configuration generation.
Old vectors cannot serve the new profile; generated descriptions can be reused.
There is no silent cross-model failover.

## Optional same-model native service

DashScope is the default, so the dcx machine needs no 8B weights or local GPU
inference. For another GPU machine, the [deployment guide](../services/qwen-native/README.md)
provides an actual `Qwen3-VL-Embedding-8B` joint endpoint:

```toml
[deepdocx.index]
joint_protocol = "openai"
joint_base_url = "https://your-inference-host/v1"
joint_key_env = "QWEN_VL_JOINT_API_KEY"
joint_model = "Qwen3-VL-Embedding-8B"
joint_dimensions = 1024
```

Spaces remain isolated even with a same-model deployment, since revisions,
instructions, and preprocessing may differ. dcx does not automatically download
weights or use its host GPU.

References: [Gitee API](https://ai.gitee.com/docs/openapi/v1),
[DashScope multimodal embeddings](https://help.aliyun.com/zh/model-studio/multimodal-embedding-api-reference),
[Qwen model card](https://huggingface.co/Qwen/Qwen3-VL-Embedding-8B).
