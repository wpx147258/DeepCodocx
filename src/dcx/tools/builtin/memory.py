"""The Remember tool: agent-initiated, strictly-scoped memory writes.

There is no background extraction. The main agent is the only writer, and only
when the user's own prompt contains something durable. The schema and the
description are deliberately opinionated: most turns should not call this.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...context.memory import (
    MEMORY_TYPES,
    MemoryRecord,
    MemoryStore,
    looks_like_secret,
)
from ..base import ToolContext, ToolResult, ToolSpec


class RememberTool:
    spec = ToolSpec(
        name="Remember",
        description=(
            "Save one durable memory for future sessions. Call this rarely, and only "
            "when the user's own message states a lasting preference, constraint, "
            "correction, or external reference that future work must honor. "
            "Do not save task state, plans, progress, tool output, repository facts, "
            "or anything that can be re-read from the code. Most turns need no call; "
            "one short record beats several. Reuse an existing name to update it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short kebab-case identifier, e.g. 'prefers-tabs'.",
                },
                "description": {
                    "type": "string",
                    "description": "One line summary for the memory catalog.",
                },
                "type": {
                    "type": "string",
                    "enum": list(MEMORY_TYPES),
                    "description": "user | feedback | project | reference.",
                },
                "content": {
                    "type": "string",
                    "description": "The durable fact plus why it matters.",
                },
            },
            "required": ["name", "type", "content"],
            "additionalProperties": False,
        },
        category="write",
    )

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG002
        name = str(args["name"]).strip()
        content = str(args["content"]).strip()
        description = str(args.get("description", "")).strip()
        memory_type = str(args["type"])
        if not name or not content:
            return ToolResult.error("Error: name and content must be non-empty")
        if memory_type not in MEMORY_TYPES:
            return ToolResult.error(
                f"Error: unknown memory type {memory_type!r}; use one of "
                + ", ".join(MEMORY_TYPES)
            )
        if looks_like_secret(content) or looks_like_secret(description):
            return ToolResult.error(
                "Error: refusing to store what looks like a credential"
            )

        existing = {record.name.lower() for record in self._store.all()}
        action = "Updated" if name.lower() in existing else "Saved"
        try:
            path = self._store.save(
                MemoryRecord(
                    name=name,
                    description=description,
                    type=memory_type,
                    content=content,
                )
            )
        except (OSError, ValueError) as exc:
            return ToolResult.error(f"Error: could not save memory: {exc}")
        return ToolResult.text(f"{action} memory {name!r} ({memory_type}) in {path.name}")
