"""Stable provider-facing tools over a private, discoverable MCP catalog."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator

from ..tools.base import ToolContext, ToolResult, ToolSpec
from .client import ServerConfig, StdioClient, normalize_result


class ExternalTool:
    def __init__(self, client: StdioClient, raw: dict[str, Any]) -> None:
        self.client = client
        self.name = str(raw["name"])
        schema = raw.get("inputSchema", {"type": "object"})
        Draft202012Validator.check_schema(schema)
        self.spec = ToolSpec(
            name=f"{client.config.name}.{self.name}",
            description=str(raw.get("description", ""))[:4000],
            input_schema=schema,
            category="read" if self.name in client.config.read_tools else "meta",
        )

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        payload = await self.client.request(
            "tools/call", {"name": self.name, "arguments": dict(args)}
        )
        return normalize_result(payload)


class ExternalCatalog:
    def __init__(self, servers: list[ServerConfig]) -> None:
        self.clients = [StdioClient(config) for config in servers]
        self._tools: dict[str, ExternalTool] = {}
        self._loaded: set[str] = set()
        self._lock = asyncio.Lock()

    async def load(self) -> list[str]:
        errors: list[str] = []
        async with self._lock:
            for client in self.clients:
                if client.config.name in self._loaded:
                    continue
                try:
                    items = [ExternalTool(client, raw) for raw in await client.tools()]
                    for tool in items:
                        self._tools[tool.spec.name] = tool
                    self._loaded.add(client.config.name)
                except Exception as exc:
                    errors.append(f"{client.config.name}: {type(exc).__name__}: {exc}")
        return errors

    async def discover(self, query: str, limit: int) -> ToolResult:
        errors = await self.load()
        terms = set(re.findall(r"[\w]+", query.casefold()))

        def score(tool: ExternalTool) -> int:
            haystack = (tool.spec.name + " " + tool.spec.description).casefold()
            return sum(1 for term in terms if term in haystack)

        ordered = sorted(self._tools.values(), key=lambda tool: (-score(tool), tool.spec.name))
        selected = [tool for tool in ordered if score(tool) > 0] if terms else ordered
        if not selected:
            selected = ordered
        return ToolResult.text(
            json.dumps(
                {
                    "tools": [
                        {
                            "tool": tool.spec.name,
                            "description": tool.spec.description,
                            "inputSchema": dict(tool.spec.input_schema),
                        }
                        for tool in selected[:limit]
                    ],
                    "warnings": errors,
                    "available": len(ordered),
                },
                ensure_ascii=False,
            )
        )

    async def resolve(self, name: str) -> ExternalTool:
        errors = await self.load()
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(
                f"Unknown external tool {name!r}; use DiscoverTools. {'; '.join(errors)}"
            )
        return tool

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for client in self.clients))


class DiscoverTools:
    spec = ToolSpec(
        name="DiscoverTools",
        description="Discover external tools on demand. Returns descriptions and input schemas; use CallExternalTool to invoke a result.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "additionalProperties": False,
        },
        category="read",
    )

    def __init__(self, catalog: ExternalCatalog) -> None:
        self.catalog = catalog

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        return await self.catalog.discover(str(args.get("query", "")), int(args.get("limit", 5)))


class CallExternalTool:
    spec = ToolSpec(
        name="CallExternalTool",
        description="Invoke a discovered external tool by its exact reference and arguments. Its actual schema and permissions are checked before execution.",
        input_schema={
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "arguments": {"type": "object"},
            },
            "required": ["tool", "arguments"],
            "additionalProperties": False,
        },
        category="meta",
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult.error("External tool resolver is not configured")
