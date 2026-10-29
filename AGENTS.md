# DeepCodocx development notes

## Architecture

- `src/dcx/runtime.py` contains the only agent loop. Do not add a second loop.
- `src/dcx/assembly.py` is the only place that constructs concrete providers,
  tools, stores, and pipelines. Other modules depend on protocols.
- System-prompt sections live in `src/dcx/context/prompts/`. Each module must
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
- Tool concurrency is opt-in per tool (`concurrency_safe`). Authorization and permission
  prompts stay serialized in call order; the runtime may only parallelize consecutive
  safe calls, and results must keep the model's call order.
- Background helpers (summarization) are best-effort: retry `retryable` provider
  errors, cap total time, and log a single warning rather than a traceback. Do not let
  a background failure surface as an agent turn error.
- Memory is written only by the model through the `Remember` tool, with strict
  criteria (durable user preferences, not task state). Do not reintroduce background
  extraction or per-turn summarization of the whole conversation. Records are
  injected into the newest user message, never into the system prompt: a changing
  system section invalidates the cached prefix.
- Session records are append-only and must stay forward compatible: ignore unknown
  record types and unknown fields. A resumed session restores its recorded permission
  mode unless an explicit CLI flag overrides it. Forking copies a session directory
  under a new id; never mutate the source session.
- The shell danger guard is approval UX, not a sandbox. Keep the pattern list short and
  high precision, never let `always`, bypass, or `/auto` skip it, and fail closed when
  there is no interactive asker.
- Provider usage: `input_tokens` excludes the cached prefix. Prompt size is
  input + cache_read + cache_write; calibrate the estimator and report context on that
  sum, never on `input_tokens` alone.
- History is append-only. Never rewrite earlier messages to save tokens: a changed
  prefix invalidates the provider's cache and costs a full re-read of the conversation.
  Spill oversized tool output once at creation (the transcript stores the pointer), and
  rewrite history only through a summarization checkpoint.
- Headless JSON output is a public surface: stdout carries data only, diagnostics
  go to stderr, and every completed turn ends with a `result` object. Keep event
  and field names stable; use `stop_reason`/`is_complete` for completion.
- The web host (`src/dcx/web/`) is another adapter, not a second agent. It may
  read runtime state and drive one turn at a time, but never appends messages, edits
  usage, or bypasses permission policy. Browser permission prompts fail closed when
  the client disappears or times out. Session switching closes the old runtime and
  rebuilds through `assembly.py`; the SSE event names are a public surface like JSONL.
  Serve the page with a strict CSP and keep the default loopback bind. Non-local
  binds require the API token; never disable it silently. UI-only preferences (sidebar
  width, search text, theme) stay client-side; chat mutations go through the API.
- Code checkpoints live under the session's `checkpoints/` directory and snapshot
  files only before Write/Edit runs in a user turn. Restoring must validate the
  current post-hash, refuse to overwrite manual changes unless explicitly forced,
  and never follow symlinks. Bash effects are not reversible; surface that warning.
  Rewind the transcript only after the code restore decision, and keep fork/delete
  behaviour consistent with the session directory.
- UI-only session state (custom title, archived flag) lives in `meta.json` and is
  changed atomically; never rewrite `session.jsonl` for display concerns, because that
  would invalidate the prompt cache. Archive is reversible; delete removes the session
  directory and must refuse the current session.

- Add a test with every new behavior. Loop tests use `tests/support/fake_provider.py`
  and never hit the network.
