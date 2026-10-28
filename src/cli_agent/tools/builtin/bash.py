"""Bash tool with timeouts, process-group cleanup, and bounded output.

Output is read as it arrives and capped per stream, so a command cannot force
the harness to buffer its full output before truncation. Process-group cleanup
is POSIX-only; Windows falls back to killing the shell process.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from ..base import ToolContext, ToolResult, ToolSpec

MAX_OUTPUT_BYTES = 50_000
_STREAM_LIMIT = MAX_OUTPUT_BYTES // 2
_CHUNK_SIZE = 8_192
_MARKER_RESERVE = 64


@dataclass
class _BoundedOutput:
    limit: int
    total: int = 0
    overflow: bool = False
    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.limit - len(self.head)
        if room > 0:
            self.head.extend(chunk[:room])
        self.tail.extend(chunk)
        if len(self.tail) > self.limit:
            del self.tail[: -self.limit]
        if self.total > self.limit:
            self.overflow = True

    def text(self) -> str:
        if not self.overflow:
            return bytes(self.head).decode("utf-8", errors="replace")
        half = max(1, (self.limit - _MARKER_RESERVE) // 2)
        head_bytes = bytes(self.head[:half])
        tail_bytes = bytes(self.tail[-half:])
        omitted = max(0, self.total - len(head_bytes) - len(tail_bytes))
        return (
            f"{head_bytes.decode('utf-8', errors='replace')}"
            f"\n... [{omitted} bytes omitted] ...\n"
            f"{tail_bytes.decode('utf-8', errors='replace')}"
        )


def _popen_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the process group even if the leader has already exited.

    A shell can exit while grandchildren still hold stdout/stderr open.
    """
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

        stdout_pipe = cast(asyncio.StreamReader, proc.stdout)
        stderr_pipe = cast(asyncio.StreamReader, proc.stderr)
        stdout = _BoundedOutput(_STREAM_LIMIT)
        stderr = _BoundedOutput(_STREAM_LIMIT)
        killed_for_output = False

        async def read_stream(
            stream: asyncio.StreamReader, output: _BoundedOutput
        ) -> None:
            nonlocal killed_for_output
            while True:
                chunk = await stream.read(_CHUNK_SIZE)
                if not chunk:
                    break
                output.feed(chunk)
                if output.overflow and not killed_for_output:
                    killed_for_output = True
                    _kill_tree(proc)

        async def consume() -> int:
            tasks = [
                asyncio.create_task(read_stream(stdout_pipe, stdout)),
                asyncio.create_task(read_stream(stderr_pipe, stderr)),
            ]
            try:
                await asyncio.gather(*tasks)
                return await proc.wait()
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        try:
            returncode = await asyncio.wait_for(consume(), timeout=timeout)
        except TimeoutError:
            _kill_tree(proc)
            await _reap(proc)
            return ToolResult.error(f"Error: command timed out after {timeout}s")
        except asyncio.CancelledError:
            _kill_tree(proc)
            await _reap(proc)
            raise
        except BaseException:
            _kill_tree(proc)
            await _reap(proc)
            raise

        output = (stdout.text() + stderr.text()).strip()
        truncated = stdout.overflow or stderr.overflow
        if truncated:
            # The exit status reflects our kill, not the command's result.
            output = (output + "\n[output truncated; command terminated]").strip()
        elif returncode:
            suffix = f"\n[exit code: {returncode}]"
            output = (output + suffix) if output else suffix.strip()
        return ToolResult.text(output or "(no output)")


async def _reap(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(proc.wait(), timeout=5)
