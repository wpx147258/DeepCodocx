"""Workspace location, environment, and path conventions."""

from __future__ import annotations

import os
import platform
from datetime import date

from ...providers.base import SystemSection
from .types import PromptContext

_SHELL = os.environ.get("SHELL", "unknown")


def build(context: PromptContext) -> SystemSection:
    windows_note = (
        "\n- On Windows the Bash tool runs through cmd.exe; use cmd.exe syntax."
        if os.name == "nt"
        else ""
    )
    text = f"""Workspace:
- Current working directory: {context.cwd}
- Platform: {platform.platform()}
- OS: {os.name}
- Shell: {_SHELL}
- Date: {date.today().isoformat()}

Paths:
- Relative paths resolve from the workspace root above.
- Absolute paths identify files or directories on the host.
- A trailing slash marks a directory; list it with Glob or Bash when its contents
  matter.
- Treat a path without a trailing slash as a file unless the environment shows
  otherwise. Use Read when its contents are needed, and do not claim to have
  inspected it before reading.
- A token starting with @ (for example @src/app.py) is a path the user explicitly
  referenced. @"..." quotes a path containing spaces. Resolve relative paths from
  the workspace root and pass the path without the leading @ and without the quotes
  to tools. The harness does not preprocess it.{windows_note}"""
    return SystemSection("workspace", text, cacheable=True)
