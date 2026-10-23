# cli-agent development notes

## Architecture

- `src/cli_agent/runtime.py` contains the only agent loop. Do not add a second loop.
- `src/cli_agent/assembly.py` is the only place that constructs concrete providers,
  tools, stores, and pipelines. Other modules depend on protocols.
- System-prompt sections live in `src/cli_agent/context/prompts/`. Each module must
  claim only capabilities the harness actually has; the tool list is generated from
  the live `ToolSpec` registry.
- Internal messages are provider-neutral (`messages.py`). Vendor wire formats belong in
  `providers/`.
- The CLI owns input and rendering only. It must never append to the transcript, mutate
  the model, or count usage.
- Runtime state lives under XDG data directories. Do not write state into the project.

## Rules

- Every tool result is data: unknown tools, invalid input, permission denials, and
  exceptions all become `ToolResult(is_error=True)`.
- Validate tool input with JSON Schema before executing.
- Keep provider selection explicit. Never infer a provider from a model prefix.
- Extended thinking is a provider capability: declare `supports_thinking` on the
  profile and send the vendor `thinking` parameter only from the adapter. When
  enabled, replay signed reasoning blocks; the budget must be below max output.
- Keep prompts free of product branding; provider-specific wording belongs in the
  adapter.
- Background helpers (summarization, memory extraction) are best-effort: retry
  `retryable` provider errors, cap total time, and log a single warning rather than a
  traceback. Do not let a background failure surface as an agent turn error.
- Add a test with every new behavior. Loop tests use `tests/support/fake_provider.py`
  and never hit the network.
