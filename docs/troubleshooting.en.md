# Troubleshooting

[简体中文](troubleshooting.md) | **English** · [Home](../README.en.md)

## No graphical browser in WSL

Use `--no-open`, then open the printed URL manually in your Windows browser:

```bash
uv run dcx web --provider deepseek --no-open
```

## Address already in use

Another process is listening on the port. Keep it running and choose a different
port for DeepCodocx:

```bash
uv run dcx web --provider deepseek --port 8766 --no-open
```

On Linux / WSL, `ss -ltnp 'sport = :8765'` shows the current listener.

## Virtual environment breaks after moving the project

Activation scripts and installed commands contain absolute paths. Rebuild the
environment after moving the directory. Leave the old environment first, then
run these commands in the new project directory:

```bash
deactivate
uv venv --clear --prompt dcx .venv
uv sync --extra dev
source .venv/bin/activate
```

Use `deactivate` only when an environment is active. Activation is optional;
`uv run dcx ...` can use the project environment directly. The package is
`dcx`, so the entry point in `pyproject.toml` must target `dcx.entry:main`.

## Upgrading from cli-agent to dcx

After updating the code, run `uv sync` and use `uv run dcx ...`. The distribution,
executable, and Python package are all named `dcx`; the old `cli-agent` command is
no longer installed. Changing the command name alone does not change the project ID.

Configuration uses `~/.config/dcx` and runtime data uses `~/.local/share/dcx`,
respecting the corresponding XDG variables. If a new directory is absent but its
legacy `cli-agent` directory exists, startup creates a `dcx → cli-agent` symlink.
Original files stay in place, preserving absolute history pointers, session locks,
and library selections. **Keep the legacy directory: it still holds your data.**
If both directories exist, `dcx` takes precedence without merging or overwriting
either store. Failure to create an alias is reported instead of silently starting
with empty history.

At the same browser origin, old drafts, themes, sidebar preferences, and the
current tab's token migrate automatically to `dcx-*` keys. Existing new values
take precedence. A different hostname or port uses separate browser storage.

## Session history disappears after moving the project directory

The project ID is the first 12 characters of the SHA-256 hash of the absolute
working-directory path. Moving or renaming the directory changes its history namespace.
The original files remain under
`$XDG_DATA_HOME/dcx/projects/<old-project-id>/sessions/`; the default data
directory is `~/.local/share/dcx`.

Before migrating, stop related session writes and back up the data. Copy session
directories with non-conflicting IDs into the new project's sessions directory.
Preserve IDs, records, resources, and timestamps. Compare conflicting
directories instead of overwriting existing sessions. Review memory and
checkpoints separately; checkpoints validate their workspace and content
hashes.

General and DeepDocx sessions have separate lists, and empty sessions are
hidden. Refresh the page after recovery to reload the list. Data accessed through
a compatibility symlink still resides in its original `cli-agent` directory.

## Empty document library list

Connect a preprocessed bundle directory that the server can access. The
`paper-bundles` root and a `data` directory containing it are both accepted. You
can set a directory in configuration:

```toml
[deepdocx]
default_library = "/path/to/paper-bundles"
```

A successful selection is remembered. When no directory is saved or configured,
a nearby `deep-docs-unified/data` can be discovered. If no valid library is
available, the dialog expands its directory input. Web/CLI ingestion notifies
readers to refresh on the next read. Restricted sessions need a scope change to
include new documents outside that scope. A new session rediscovers externally
copied bundles; semantic indexes need a separate manual sync.

## Ingestion succeeded, but semantic search returns nothing

Ingestion publishes source bundles and the full-text catalog. Vectors need a
separate sync. Inspect coverage, services, and job errors in Index management,
or run:

```bash
dcx docs index status --library /path/to/paper-bundles
dcx docs index sync --library /path/to/paper-bundles
```

By default, the first manual sync enrolls that library in automatic synchronization
after future ingestion. Credentials, resuming a session, and externally copying
bundles do not trigger a build. Check the selected library and document scope.
Use keyword `doc_search` without a current index; empty semantic results do not
prove that the paper lacks relevant content.

## Credentials exist, but services report missing keys or request failures

All encoders default to `DASHSCOPE_API_KEY`; descriptions use `GITEE_API_KEY`.
They are independent of conversational credentials such as
`DEEPSEEK_API_KEY`. Restart Web/MCP processes after exporting keys. Editing
`.bashrc` or setting variables in another terminal does not update running
processes. Custom `api_key_env`/`joint_key_env` settings require the corresponding
variable names.

“Configured” checks settings and variable presence, not authorization, balance,
or model availability. Inspect job errors and `worker.log`, and verify endpoint
and region. DashScope uses `qwen3-vl-embedding`, not Gitee's
`Qwen3-VL-Embedding-8B`. Their model IDs and dimension specifications are not
interchangeable.

## Some caption, description, or joint counts are zero

To avoid a full-library job, use **选择论文** (Select papers), search for a title,
and select it, or repeat CLI `--doc DOC_ID`. Scoped sync preserves other indexed
papers. Both DashScope encoders now default to 2560 dimensions. If a configuration
still explicitly specifies the old Gitee encoder, remove those overrides or use
the new defaults, restart the host, and sync. Old explicit settings are not
silently interpreted as DashScope.

Basic usually provides page images; Marker can provide more figure crops and
original captions. Missing original captions can leave `caption_original` and
its joint representation empty. Disabling descriptions omits generated-caption
representations. Joint encoding also needs an available DashScope service.
Inspect document states and actual errors before syncing missing work; different
counts across the four families do not by themselves mean failure.

## Documents are partial/pending, or the worker stopped

Source or configuration changes invalidate old generations, which queries
exclude. Partial documents can retain usable representations. Ordinary sync
reuses caches and retries; `--force` is not needed by default. Repair damaged
source files, then sync again. Unexpected worker exits are recorded as failure;
jobs are not automatically restarted, so synchronize manually.

Closing a dialog or stopping a conversation does not stop indexing. Use the
index panel's stop action or:

```bash
dcx docs index cancel --library /path/to/paper-bundles JOB_ID
```

Logs live at
`$XDG_DATA_HOME/dcx/semantic/<library-id>/jobs/<job-id>/worker.log`. There is no
filesystem watcher: synchronize external additions manually. Moving the library
root creates a new library ID and also requires a sync. See
[multimodal indexes](multimodal-index.en.md) for lifecycle and status fields.

## The figure is visible, but the model does not analyze it

Browser previews and model input are separate paths. The model must receive an
actual image through `doc_view` or Read, and the selected profile, model, and
endpoint must support images. Inspect the tool output to confirm that image
blocks were returned.
Image retrieval encodes and locates similar evidence; it does not automatically
send the matching images to the answer model.
