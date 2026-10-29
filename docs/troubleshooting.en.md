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
available, the dialog expands its directory input. Create a new document session
to discover newly converted bundles.

## The figure is visible, but the model does not analyze it

Browser previews and model input are separate paths. The model must receive an
actual image through `doc_view` or Read, and the selected profile, model, and
endpoint must support images. Inspect the tool output to confirm that image
blocks were returned.
