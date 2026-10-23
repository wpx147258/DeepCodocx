"""Bash tool with timeouts, process-group cleanup, and output caps."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping
from typing import Any

from ..base import ToolContext, ToolResult, ToolSpec

MAX_OUTPUT_CHARS = 50_000


def _cap(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return f"{text[:half]}\n... [{omitted} characters omitted] ...\n{text[-half:]}"


def _popen_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()


class BashTool:
    spec = ToolSpec(
        name="Bash",
        description=(
            "Run a shell command in the workspace. Returns combined stdout and stderr. "
            "Use it to inspect files, run tests, and execute programs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 600,
                    "description": "Timeout in seconds (default 120).",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        category="shell",
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args["command"])
        timeout = min(int(args.get("timeout", 120)), 600)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(ctx.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_popen_kwargs(),
            )
        except OSError as exc:
            return ToolResult.error(f"Error: could not start shell: {exc}")

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            _kill_tree(proc)
            await _reap(proc)
            return ToolResult.error(f"Error: command timed out after {timeout}s")
        except asyncio.CancelledError:
            _kill_tree(proc)
            await _reap(proc)
            raise

        output = (stdout + stderr).decode("utf-8", errors="replace").strip()
        output = _cap(output)
        if proc.returncode:
            suffix = f"\n[exit code: {proc.returncode}]"
            output = (output + suffix) if output else suffix.strip()
        return ToolResult.text(output or "(no output)")


async def _reap(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(proc.wait(), timeout=5)
