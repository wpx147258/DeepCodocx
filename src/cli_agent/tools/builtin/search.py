"""Glob and Grep tools.

Grep prefers ripgrep (which respects .gitignore and skips binaries) and falls
back to a small Python implementation when ``rg`` is unavailable.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..base import ToolContext, ToolResult, ToolSpec

MAX_OUTPUT_CHARS = 50_000
MAX_GLOB_RESULTS = 500
DEFAULT_HEAD_LIMIT = 200
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
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


def _cap(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n... [output truncated] ...\n"


class GlobTool:
    spec = ToolSpec(
        name="Glob",
        description=(
            "Find files by glob pattern (for example '**/*.py'). Returns paths relative "
            "to the workspace, sorted."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern."},
                "path": {
                    "type": "string",
                    "description": "Directory to search (default: workspace root).",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        category="read",
        path_arg="path",
        concurrency_safe=True,
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args["pattern"])
        root = _resolve(ctx, str(args.get("path", ".")))
        if not root.exists():
            return ToolResult.error(f"Error: path does not exist: {root}")
        if not root.is_dir():
            return ToolResult.error(f"Error: not a directory: {root}")
        try:
            matches: list[Path] = []
            for item in root.glob(pattern):
                if item.is_file():
                    matches.append(item)
                    if len(matches) >= MAX_GLOB_RESULTS:
                        break
        except (ValueError, OSError) as exc:
            return ToolResult.error(f"Error: invalid glob: {exc}")
        if not matches:
            return ToolResult.text("No files matched")
        rendered = "\n".join(sorted(_display(ctx, item) for item in matches))
        if len(matches) >= MAX_GLOB_RESULTS:
            rendered += f"\n... [stopped after {MAX_GLOB_RESULTS} matches] ..."
        return ToolResult.text(_cap(rendered))


class GrepTool:
    spec = ToolSpec(
        name="Grep",
        description=(
            "Search file contents with a regular expression. Uses ripgrep when available "
            "and falls back to Python otherwise."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression."},
                "path": {
                    "type": "string",
                    "description": "File or directory to search (default: workspace root).",
                },
                "glob": {
                    "type": "string",
                    "description": "Only search files matching this glob, e.g. '*.py'.",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                    "description": "Output shape (default: files_with_matches).",
                },
                "head_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2000,
                    "description": "Maximum output lines (default 200).",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        category="read",
        path_arg="path",
        concurrency_safe=True,
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args["pattern"])
        root = _resolve(ctx, str(args.get("path", ".")))
        if not root.exists():
            return ToolResult.error(f"Error: path does not exist: {root}")
        file_glob = args.get("glob")
        file_glob = str(file_glob) if file_glob else None
        output_mode = str(args.get("output_mode", "files_with_matches"))
        head_limit = max(1, int(args.get("head_limit", DEFAULT_HEAD_LIMIT)))

        rg = shutil.which("rg")
        if rg:
            return await self._ripgrep(rg, pattern, root, file_glob, output_mode, head_limit, ctx)
        try:
            output = await asyncio.to_thread(
                _python_grep, pattern, root, file_glob, output_mode, head_limit, ctx
            )
        except re.error as exc:
            return ToolResult.error(f"Error: invalid regular expression: {exc}")
        return ToolResult.text(_cap(output or "No matches found"))

    async def _ripgrep(
        self,
        rg: str,
        pattern: str,
        root: Path,
        file_glob: str | None,
        output_mode: str,
        head_limit: int,
        ctx: ToolContext,
    ) -> ToolResult:
        command = [rg, "--no-heading", "-n"]
        if output_mode == "files_with_matches":
            command.append("-l")
        elif output_mode == "count":
            command.append("-c")
        if file_glob:
            command.extend(["--glob", file_glob])
        command.extend([pattern, str(root)])
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except TimeoutError:
            return ToolResult.error("Error: grep timed out after 30s")
        except OSError as exc:
            return ToolResult.error(f"Error: could not run ripgrep: {exc}")
        if proc.returncode == 1:
            return ToolResult.text("No matches found")
        if proc.returncode not in (0, 1):
            return ToolResult.error(
                f"Error: ripgrep failed: {stderr.decode('utf-8', errors='replace').strip()}"
            )
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        if output_mode == "content":
            lines = lines[:head_limit]
            lines = [_rebase_line(line, root, ctx) for line in lines]
        else:
            lines = lines[:head_limit]
            lines = [_rebase_path(line, root, ctx) for line in lines]
        return ToolResult.text(_cap("\n".join(lines) or "No matches found"))


def _rebase_path(line: str, root: Path, ctx: ToolContext) -> str:
    text = line.strip()
    path = Path(text)
    if not path.is_absolute():
        path = root / path
    return _display(ctx, path)


def _rebase_line(line: str, root: Path, ctx: ToolContext) -> str:
    parts = line.split(":", 2)
    if len(parts) != 3:
        return line
    path_text, number, rest = parts
    path = Path(path_text)
    if not path.is_absolute():
        path = root / path
    return f"{_display(ctx, path)}:{number}:{rest}"


def _python_grep(
    pattern: str,
    root: Path,
    file_glob: str | None,
    output_mode: str,
    head_limit: int,
    ctx: ToolContext,
) -> str:
    regex = re.compile(pattern)
    lines: list[str] = []
    files = [root] if root.is_file() else _walk_files(root)
    for path in files:
        relative = _display(ctx, path)
        if file_glob and not fnmatch.fnmatch(relative, file_glob) and not fnmatch.fnmatch(
            path.name, file_glob
        ):
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        text = raw.decode("utf-8", errors="replace")
        count = 0
        for number, line in enumerate(text.splitlines(), start=1):
            if not regex.search(line):
                continue
            count += 1
            if output_mode == "content":
                lines.append(f"{relative}:{number}:{line}")
            elif output_mode == "count":
                continue
            else:
                lines.append(relative)
                break
        if output_mode == "count" and count:
            lines.append(f"{relative}:{count}")
        if len(lines) >= head_limit:
            break
    return "\n".join(lines[:head_limit])


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS:
                    stack.append(entry)
            elif entry.is_file():
                files.append(entry)
    return files
