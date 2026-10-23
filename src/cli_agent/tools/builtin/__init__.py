"""The v1 built-in tool set."""

from __future__ import annotations

from ..registry import ToolRegistry
from .bash import BashTool
from .files import EditTool, ReadTool, WriteTool
from .search import GlobTool, GrepTool
from .skills import LoadSkillTool, SkillSource


def build_builtin_registry(skill_source: SkillSource) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        BashTool(),
        ReadTool(),
        WriteTool(),
        EditTool(),
        GlobTool(),
        GrepTool(),
        LoadSkillTool(skill_source),
    ):
        registry.register(tool)
    return registry
