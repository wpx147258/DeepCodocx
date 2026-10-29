"""Name -> tool registry.

Only this stable inventory is shown to the provider. MCP tools are resolved
privately through the fixed discovery/call entries, preserving the system prefix.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import Tool, ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec for tool in self._tools.values())

    def snapshot(self) -> tuple[Tool, ...]:
        return tuple(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())
