# Development guide

[简体中文](development.md) | **English** · [Home](../README.en.md)

## Environment and checks

```bash
uv sync --extra dev
uv run ruff check src tests
uv run mypy src
uv run pytest -q --cov=dcx --cov-report=term-missing --cov-fail-under=75
node --check src/dcx/web/static/app.js
```

The source package is `src/dcx`; the installed command is `cli-agent`. The CLI,
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
```

This check uses a scripted provider with the real Web server, stdio MCP, and
browser. It covers default libraries, remembered selection, fork/new, search,
images, citations, session isolation, and narrow layouts.

Screenshots in the output directory support visual inspection. Add
`--library /path/to/paper-bundles` to check a real local corpus containing
MEMORYVLA Marker bundles. The corpus is accessed read-only.

## Live-provider tests

```bash
export CLI_AGENT_E2E_PROVIDER=deepseek
export DEEPSEEK_API_KEY="your-api-key"
uv run pytest tests/e2e -q
```

These tests make real provider requests and are skipped when no provider is
configured. Set `CLI_AGENT_E2E_MODEL` to choose the model. The manual CI
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
- Cover resume, cancellation, failures, and cleanup while maintaining
  compatibility with existing session records.

Public documentation covers product usage, configuration, architecture, and
contribution. Personal plans, learning notes, and review records belong in
Git-ignored local directories and are excluded from release commits.
