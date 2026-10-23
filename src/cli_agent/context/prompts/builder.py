"""Compose the system prompt from ordered, independent modules."""

from __future__ import annotations

from collections.abc import Callable

from ...providers.base import SystemSection
from . import context as context_sections
from . import identity, policies, tools, workspace
from .types import PromptContext

PromptModule = Callable[[PromptContext], SystemSection | None]

_MODULES: tuple[PromptModule, ...] = (
    identity.build,
    workspace.build,
    tools.build,
    policies.build_files,
    policies.build_shell,
    context_sections.build_project,
    context_sections.build_memory,
    context_sections.build_skills,
    context_sections.build_compaction,
)


class PromptBuilder:
    def __init__(self, modules: tuple[PromptModule, ...] = _MODULES) -> None:
        self.modules = modules

    def build(self, context: PromptContext) -> tuple[SystemSection, ...]:
        sections: list[SystemSection] = []
        for module in self.modules:
            section = module(context)
            if section is not None and section.text.strip():
                sections.append(section)
        return tuple(sections)
