"""Read, Write, and Edit tools.

Write is atomic. Edit preserves the original bytes as far as possible, refuses
ambiguous replacements, and returns a unified diff so the model can verify it.
"""

from __future__ import annotations

import base64
import contextlib
import difflib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...messages import ImageBlock
from ..base import ToolContext, ToolResult, ToolSpec

MAX_READ_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_READ_CHARS = 100_000
_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _resolve(ctx: ToolContext, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ctx.cwd / path
    return path.resolve(strict=False)


def _display(ctx: ToolContext, path: Path) -> str:
    try:
        return str(path.relative_to(ctx.cwd.resolve()))
    except ValueError:
        return str(path)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


class ReadTool:
    spec = ToolSpec(
        name="Read",
        description=(
            "Read a text file. Returns numbered lines. Use offset and limit for large files. "
            "Image files return an image block when supported."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, relative to the workspace."},
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "First line to return (1-based, default 1).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20_000,
                    "description": "Maximum lines to return (default 2000).",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        category="read",
        path_arg="path",
        concurrency_safe=True,
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx, str(args["path"]))
        if not path.exists():
            return ToolResult.error(f"Error: file does not exist: {path}")
        if not path.is_file():
            return ToolResult.error(f"Error: not a file: {path}")
        try:
            size = path.stat().st_size
        except OSError as exc:
            return ToolResult.error(f"Error: cannot stat file: {exc}")
        if size > MAX_READ_BYTES:
            return ToolResult.error(
                f"Error: file is too large ({size} bytes); use a narrower file or offset/limit"
            )

        suffix = path.suffix.lower()
        if suffix in _IMAGE_TYPES:
            if size > MAX_IMAGE_BYTES:
                return ToolResult.error(f"Error: image is too large ({size} bytes)")
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return ToolResult((ImageBlock(_IMAGE_TYPES[suffix], data),))

        try:
            raw = path.read_bytes()
        except OSError as exc:
            return ToolResult.error(f"Error: cannot read file: {exc}")
        if b"\x00" in raw:
            return ToolResult.error("Error: binary file; use Bash if you need to inspect it")

        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        offset = max(1, int(args.get("offset", 1)))
        limit = max(1, int(args.get("limit", 2000)))
        if not lines:
            return ToolResult.text("(empty file)")
        if offset > len(lines):
            return ToolResult.error(
                f"Error: offset {offset} is past the end of the file ({len(lines)} lines)"
            )
        selected = lines[offset - 1 : offset - 1 + limit]

        rendered: list[str] = []
        for number, line in enumerate(selected, start=offset):
            if not line.endswith("\n"):
                line += "\n"
            rendered.append(f"{number:6d}\t{line}")
        output = "".join(rendered)
        if len(output) > MAX_READ_CHARS:
            output = output[:MAX_READ_CHARS] + "\n... [read output truncated] ...\n"
        if len(lines) > offset - 1 + limit:
            output += f"\n... [{len(lines) - (offset - 1 + limit)} more lines] ...\n"
        return ToolResult.text(output or "(empty file)")


class WriteTool:
    spec = ToolSpec(
        name="Write",
        description="Write a file atomically. Creates parent directories when needed.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, relative to the workspace."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        category="write",
        path_arg="path",
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx, str(args["path"]))
        if path.is_dir():
            return ToolResult.error(f"Error: path is a directory: {path}")
        content = str(args["content"])
        try:
            _atomic_write(path, content.encode("utf-8"))
        except OSError as exc:
            return ToolResult.error(f"Error: could not write file: {exc}")
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return ToolResult.text(f"Wrote {line_count} lines to {_display(ctx, path)}")


class EditTool:
    spec = ToolSpec(
        name="Edit",
        description=(
            "Replace an exact string in a file. old_string must be unique unless replace_all is true. "
            "Returns a unified diff."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, relative to the workspace."},
                "old_string": {"type": "string", "description": "Exact text to replace."},
                "new_string": {"type": "string", "description": "Replacement text."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence (default false).",
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
        category="write",
        path_arg="path",
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx, str(args["path"]))
        if not path.is_file():
            return ToolResult.error(f"Error: file does not exist: {path}")
        old_string = str(args["old_string"])
        new_string = str(args["new_string"])
        replace_all = bool(args.get("replace_all", False))
        if old_string == new_string:
            return ToolResult.error("Error: old_string and new_string must be different")

        try:
            content = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult.error(f"Error: could not read file as UTF-8: {exc}")

        count = content.count(old_string)
        if count == 0:
            return ToolResult.error(f"Error: old_string not found in {_display(ctx, path)}")
        if count > 1 and not replace_all:
            return ToolResult.error(
                f"Error: old_string found {count} times in {_display(ctx, path)}. "
                "Use replace_all=true or provide more context to make it unique."
            )

        new_content = content.replace(old_string, new_string) if replace_all else content.replace(
            old_string, new_string, 1
        )
        try:
            _atomic_write(path, new_content.encode("utf-8"))
        except OSError as exc:
            return ToolResult.error(f"Error: could not write file: {exc}")

        diff = "".join(
            difflib.unified_diff(
                content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=_display(ctx, path),
                tofile=_display(ctx, path),
            )
        )
        return ToolResult.text(diff or "File updated (no visible diff)")

