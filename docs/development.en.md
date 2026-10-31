# Development guide

[简体中文](development.md) | **English** · [Home](../README.en.md)

## Environment and checks

```bash
uv sync --extra dev
uv run ruff check src tests
uv run mypy src
uv run pytest -q --cov=dcx --cov-report=term-missing --cov-fail-under=75
node --check src/dcx/web/static/app.js
node --check src/dcx/web/static/diagrams.js
node --check src/dcx/web/static/indexes.js
```

The source package is `src/dcx`; the installed command is `dcx`. The CLI,
Web host, and MCP subprocesses all use this package. See
[architecture](architecture.en.md) for module boundaries and
[AGENTS.md](../AGENTS.md) for development rules.

Add a test for every new behavior. Loop tests use
`tests/support/fake_provider.py`. Document tests use small basic / Marker
fixtures and require neither a personal corpus nor an API key.

## Browser acceptance checks

```bash
uv run --with playwright python -m playwright install chromium
uv run --with playwright python -m tests.e2e.web_smoke --output /tmp/deepcodocx-browser
uv run --with playwright python -m tests.e2e.ingest_smoke --output /tmp/deepcodocx-ingest
uv run --with playwright python -m tests.e2e.semantic_smoke --output /tmp/deepcodocx-index
uv run --with playwright python -m tests.e2e.mermaid_smoke --output /tmp/deepcodocx-mermaid
uv run --with playwright python -m tests.e2e.document_search_smoke --output /tmp/deepcodocx-document-chat
```

This check uses a scripted provider with the real Web server, stdio MCP, and
browser. It covers legacy browser preference migration, default libraries, remembered selection, fork/new, search,
images, citations, session isolation, and narrow layouts.

Ingestion acceptance requires Poppler. It uses a tiny PDF with a real upload,
independent worker, and index, checks sidebar/mobile layouts and option/task
states, and asserts zero model calls or transcript changes. See the
[ingestion guide](deepdocx-ingest.en.md).

Semantic acceptance uses a local inference fixture, tiny bundles, and real index
workers. It covers family counts, caption model selection, cached incremental
sync, text previews, native multimodal requests, source inspection, and dark/mobile
layouts without paid service requests.

Mermaid acceptance uses the real renderer to check diagram/source switching,
copying, zoom/pan, themes, syntax-error fallback, streaming output, and history
restoration without a network model service.
Document chat acceptance uses the real runtime and stdio MCP with local inference
fixtures to cover attachment references, target routing, compact cards, coverage
badges, image source reading, themes, and restored history.

Screenshots in the output directory support visual inspection. For `web_smoke`, add
`--library /path/to/paper-bundles` to check a real local corpus containing
MEMORYVLA Marker bundles. The corpus is accessed read-only.

## Multimodal index development boundaries

Inside `deepdocx/semantic/`, `settings.py` owns configuration and profiles;
`samples.py` builds bounded text/image samples; `client.py` adapts inference APIs;
`store.py` owns SQLite/sqlite-vec and caches; `engine.py` synchronizes and searches;
and `service.py`/`worker.py` manage persistent tasks. `tools.py` exposes read tools
only. Administration lives in `cli/indexes.py`, `web/indexes.py`, and
`web/static/indexes.js`. See [architecture](architecture.en.md).

```bash
uv run pytest tests/unit/test_semantic.py tests/unit/test_semantic_service.py -q
```

Offline tests use the protocol fixture in `tests/support/inference_fixture.py`
to cover representations, space isolation, incremental caching, stale/deleted
sources, session scopes, partial failures, restart detection, and cancellation.
New encoders must preserve separate query encoders per space. Revise the relevant
version when preprocessing, block construction, or caption prompts change so
old generations can be identified. Keep original locators and generation
provenance; never write generated descriptions into source text or label averaged
vectors as native fusion.

Real Gitee/DashScope checks are separate from conversational-provider tests;
the latter do not validate embedding services. Validate real indexing on a small
isolated library with `docs index sync/search`, using the required credentials
and accounting for possible charges. Protocol tests of the optional same-model
service do not establish that the actual local 8B model has run. See the
[native joint service guide](../services/qwen-native/README.md).

## Live-provider tests

```bash
export DCX_E2E_PROVIDER=deepseek
export DEEPSEEK_API_KEY="your-api-key"
uv run pytest tests/e2e -q
```

These tests make real provider requests and are skipped when no provider is
configured. Set `DCX_E2E_MODEL` to choose the model. The manual CI
`workflow_dispatch` job uses the corresponding repository secret.

## Contribution guidelines

- Keep one agent loop and construct concrete dependencies in assembly.
- Declare each new tool's actual schema and permission category. Only explicitly
  safe tools may run concurrently.
- Keep vendor wire formats inside provider adapters; internal messages remain
  neutral.
- Preserve public JSONL / SSE fields and completion semantics. Keep stdout data
  separate from diagnostics.
- Treat document contents as untrusted data. External tools cannot grant their
  own permissions.
- Keep ingestion and index writers as independent system jobs; expose no build
  or caption write tools to the main agent.
- Separate bundle evidence from XDG derivatives, filtering queries by source
  fingerprint, configuration generation, and document scope.
- Cover resume, cancellation, failures, and cleanup while maintaining
  compatibility with existing session records.

Public documentation covers product usage, configuration, architecture, and
contribution. Personal plans, learning notes, review records, project proposals,
presentations, and test reports stay local under Git ignore rules and are
excluded from release commits.
