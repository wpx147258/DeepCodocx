"""Shared values for system-prompt modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...providers.base import ModelProfile
from ...tools.base import ToolSpec
from ..memory import MemoryRecord
from ..skills import Skill


@dataclass(frozen=True, slots=True)
class PromptContext:
    cwd: Path
    profile: ModelProfile
    tools: tuple[ToolSpec, ...]
    skills: tuple[Skill, ...]
    memory_catalog: tuple[str, ...]
    memories: tuple[MemoryRecord, ...]
    project_instructions: str
    active_request: str
    compaction_active: bool = False
    agent_name: str = "cli-agent"
