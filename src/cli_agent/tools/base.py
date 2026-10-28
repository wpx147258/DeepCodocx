"""Tool protocol and the small value types every tool returns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from ..messages import ImageBlock, TextBlock

type ToolCategory = Literal["read", "write", "shell", "meta"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    category: ToolCategory = "meta"
    path_arg: str | None = None
    concurrency_safe: bool = False


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: tuple[TextBlock | ImageBlock, ...]
    is_error: bool = False

    @classmethod
    def text(cls, text: str, *, is_error: bool = False) -> ToolResult:
        return cls((TextBlock(text),), is_error=is_error)

    @classmethod
    def error(cls, text: str) -> ToolResult:
        return cls((TextBlock(text),), is_error=True)

    def text_content(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextBlock))


@dataclass(frozen=True, slots=True)
class ToolContext:
    cwd: Path
    session_id: str
    data_dir: Path


class Tool(Protocol):
    spec: ToolSpec

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult: ...
