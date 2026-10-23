"""File-operation and shell policies grounded in the actual tools."""

from __future__ import annotations

import os

from ...providers.base import SystemSection
from .types import PromptContext


def build_files(context: PromptContext) -> SystemSection:
    text = """File operations:
- Use Read, not shell commands like cat, to inspect text files. Use offset and limit
  to continue reading large files.
- Do not claim to have inspected a file before reading it.
- Read an existing file before overwriting it with Write. Prefer Edit for targeted
  changes; use Write for new files or full rewrites.
- Read a file before editing it, unless you just created or edited it in this session.
- Edit replaces an exact old_string. Include enough context to make it unique, or set
  replace_all when every occurrence should change.
- Use Glob, not shell find, to discover files by path pattern.
- Use Grep, not shell grep or rg, to search file contents. Use Read on a matched
  file for surrounding context.
- Write is atomic and creates parent directories.
- When referring to a file, use its workspace-relative path. When citing a line, use
  path:line."""
    return SystemSection("file-operations", text, cacheable=True)


def build_shell(context: PromptContext) -> SystemSection:
    windows_note = (
        "\n- On Windows a killed process may settle as [exit code: 1] without a"
        "\n  signal marker; treat a bare exit 1 after an interruption as a"
        "\n  termination, not a command failure."
        if os.name == "nt"
        else ""
    )
    text = f"""Shell:
- The Bash tool runs one command at a time in the workspace root.
- The default timeout is 120 seconds; the maximum is 600 seconds.
- Non-zero exits are reported as [exit code: N]. Investigate failures before moving
  on rather than assuming the command succeeded.
- Output is capped; very large output may be truncated.
- Use the dedicated Read, Glob, and Grep tools instead of cat, find, grep, or rg.
- A cd inside one Bash call does not persist to the next call.{windows_note}"""
    return SystemSection("shell", text, cacheable=True)
