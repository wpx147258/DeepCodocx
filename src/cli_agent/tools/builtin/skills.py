"""The load_skill tool.

The system prompt always carries the skill catalog (name + description). Full
skill text is loaded only when the model asks for it by name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from ..base import ToolContext, ToolResult, ToolSpec


class SkillSource(Protocol):
    def load(self, name: str) -> str: ...

    def catalog_lines(self, limit: int | None = None) -> list[str]: ...


class LoadSkillTool:
    spec = ToolSpec(
        name="load_skill",
        description=(
            "Load the full instructions for a skill listed in the system prompt. "
            "Pass the exact skill name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name from the catalog."}
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        category="read",
    )

    def __init__(self, source: SkillSource) -> None:
        self._source = source

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG002
        name = str(args["name"])
        try:
            text = self._source.load(name)
        except KeyError:
            catalog = self._source.catalog_lines(limit=50)
            available = "\n".join(catalog) if catalog else "(no skills installed)"
            return ToolResult.error(
                f"Error: unknown skill {name!r}. Available skills:\n{available}"
            )
        except OSError as exc:
            return ToolResult.error(f"Error: could not read skill {name!r}: {exc}")
        return ToolResult.text(text)
