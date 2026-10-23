"""Skill catalog and on-demand loading.

A skill is a directory containing ``SKILL.md`` with frontmatter. Only the
catalog (name + description) goes into the system prompt; the body is returned
by the ``load_skill`` tool.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    path: Path
    source: str


class SkillLoader:
    def __init__(self, roots: list[tuple[Path, str]] | None = None) -> None:
        self._roots = roots or []
        self._skills: dict[str, Skill] | None = None

    @classmethod
    def from_paths(
        cls, global_root: Path, project_root: Path
    ) -> SkillLoader:
        return cls([(global_root, "global"), (project_root, "project")])

    def catalog(self) -> tuple[Skill, ...]:
        return tuple(self._scan().values())

    def catalog_lines(self) -> list[str]:
        return [
            f"- {skill.name}: {skill.description}" if skill.description else f"- {skill.name}"
            for skill in self.catalog()
        ]

    def load(self, name: str) -> str:
        skills = self._scan()
        try:
            skill = skills[name]
        except KeyError as exc:
            raise KeyError(name) from exc
        text = skill.path.read_text(encoding="utf-8")
        return _body(text).strip() or text.strip()

    def _scan(self) -> dict[str, Skill]:
        if self._skills is not None:
            return self._skills
        found: dict[str, Skill] = {}
        for root, source in self._roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*/SKILL.md")):
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.warning("could not read skill %s: %s", path, exc)
                    continue
                metadata = _frontmatter(text)
                name = metadata.get("name") or path.parent.name
                description = metadata.get("description", "")
                if name in found:
                    logger.warning("duplicate skill %r; %s overrides %s", name, source, found[name].source)
                found[name] = Skill(
                    name=name,
                    description=description,
                    path=path,
                    source=source,
                )
        self._skills = found
        return found


def _frontmatter(text: str) -> Mapping[str, str]:
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _body(text: str) -> str:
    if not text.startswith("---"):
        return text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[index + 1 :])
    return text
