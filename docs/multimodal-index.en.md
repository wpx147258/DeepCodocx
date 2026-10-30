# Multimodal indexes and semantic lookup

[简体中文](multimodal-index.md) | **English** · [Home](../README.en.md)

DeepDocx indexes semantic text blocks, images, captions/descriptions, and native
image–caption combinations. Retrieval returns source locations and short evidence
cards. The existing agent decides how to read, inspect, and answer; keyword
search, Bash, Read, and the single agent loop remain available.

## Models and spaces

| Capability | Service and model | Representation |
|---|---|---|
| Text blocks, images, original/generated captions | Gitee AI `Qwen3-VL-Embedding-8B` | Gitee space; 1024 dimensions by default |
| Image + original caption, image + generated description | Alibaba Cloud DashScope `qwen3-vl-embedding`, `enable_fusion=true` | Independent native space; 1024 dimensions by default |
| Image descriptions | Gitee AI `qwen3.8-flash` or `DeepSeek-V4.1-Flash-1017` | Text with generation provenance; subsequently embedded |

Gitee's current gateway accepts text and image samples independently but rejects
both in one sample. DashScope provides native fusion, with a different model ID
and dimension specification. **Equal dimensions do not imply a shared embedding
space.** Queries are encoded separately for each service, ranked by cosine within
that space, and combined using reciprocal rank fusion (RRF). Mixed image/text
queries use separate Gitee text/image lookups and a native DashScope joint lookup.

Results retain within-space similarities and matched representations. The final
`score` is a rank score, not a cross-space similarity. No averaged vectors are
used as a substitute for native fusion. A same-model deployment remains an
optional endpoint, described below.

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
3. Select **同步索引** (Synchronize). The first manual sync also enables automatic
   synchronization of newly ingested documents for this library.
4. Close the panel while work continues; persisted jobs survive host restarts.
5. Expand **试用语义检索** (Try semantic search), provide text, an image, or both,
   and open a result in the existing evidence panel.

![Index management](web/deepdocx-index-light.png)

[Dark theme](web/deepdocx-index-dark.png) · [Mobile](web/deepdocx-index-mobile.png)

**重新生成** (Regenerate) bypasses inference caches for the entire current library.
Ordinary synchronization reuses unchanged results. Hosted services may charge for
requests; initial builds and regeneration scale with text-block and image counts.
Page images are explicitly labeled as pages rather than extracted figures.

## Configuration

```toml
[deepdocx.index]
enabled = true
auto_sync = false # First manual sync enables automatic sync for that library.
base_url = "https://ai.gitee.com/v1"
api_key_env = "GITEE_API_KEY"
model = "Qwen3-VL-Embedding-8B"
dimensions = 1024
batch_size = 8
timeout = 60
block_chars = 1800
joint_protocol = "dashscope"
joint_base_url = "https://dashscope.aliyuncs.com/api/v1"
joint_key_env = "DASHSCOPE_API_KEY"
joint_model = "qwen3-vl-embedding"
joint_dimensions = 1024
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

Gitee embeddings use the fixed requested model and support 64–4096 dimensions.
DashScope native dimensions are 256, 512, 768, 1024, 1536, 2048, or 2560; the two
spaces may have different dimensions. Relevant model, preprocessing, chunking,
and prompt revision changes invalidate the previous index generation. “Configured”
service status checks configuration and credential presence; inference failures
are reported in job outcomes.

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
| New Web/CLI ingestion | Enqueue a separate index job for enrolled libraries; conversion does not wait for model enhancement |
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
dcx docs index sync --library /path/to/paper-bundles --no-captions
dcx docs index sync --library /path/to/paper-bundles --no-wait
dcx docs index cancel --library /path/to/paper-bundles JOB_ID
dcx docs index search --library /path/to/paper-bundles "memory and action"
dcx docs index search --library /path/to/paper-bundles --image figure.png
dcx docs index search --library /path/to/paper-bundles "memory" --image figure.png
```

Administration commands need no conversational provider or agent session. Waiting
sync returns 0 on success and 1 for partial/failure/cancellation. `--force` regenerates
outputs; `--config` selects a configuration file.

Agents discover `documents.doc_index_status` and `documents.doc_semantic_search`
through the existing discovery/call tools. Semantic queries accept text, an image
locator in the selected library, or an image path under the workspace/session
roots, and can filter documents, representations, and result count. They return
bounded candidates, not answers. Model tools cannot build/delete indexes or start
caption batches. Full system prompts stay independent of the index mode;
`doc_search` remains available for keyword lookup.

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
