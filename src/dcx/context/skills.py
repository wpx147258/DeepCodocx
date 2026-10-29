"""Skill catalog and on-demand loading.

A skill is a directory containing ``SKILL.md`` with frontmatter. Only the
catalog (name + description) goes into the system prompt; the body is returned
by the ``load_skill`` tool.

Project skill files are workspace-controlled data, not instructions. Reads are
bounded and re-validated so a swapped symlink cannot turn ``load_skill`` into an
arbitrary-file read.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_SKILL_NAME_CHARS = 120
MAX_SKILL_DESCRIPTION_CHARS = 500
MAX_SKILL_BODY_BYTES = 32_000
MAX_SKILL_FILE_BYTES = 64_000
MAX_FRONTMATTER_BYTES = 8_192
MAX_SKILLS = 200
MAX_CATALOG_LINES = 50
_TRUNCATED_MARKER = "\n\n... [skill truncated] ..."

# C0/C1 controls (except tab/newline, which are collapsed afterwards), DEL,
# bidi overrides, and zero-width characters.
_UNSAFE_CHARS = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f"
    r"​-‏‪-‮⁦-⁩﻿]"
)


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
    def from_paths(cls, global_root: Path, project_root: Path) -> SkillLoader:
        return cls([(global_root, "global"), (project_root, "project")])

    def catalog(self) -> tuple[Skill, ...]:
        return tuple(self._scan().values())

    def catalog_lines(self, limit: int | None = None) -> list[str]:
        skills = self.catalog()
        selected = skills[:limit] if limit is not None else skills
        lines = [
            f"- {skill.name} [{skill.source}]: {skill.description}"
            if skill.description
            else f"- {skill.name} [{skill.source}]"
            for skill in selected
        ]
        if limit is not None and len(skills) > limit:
            lines.append(f"- ... ({len(skills) - limit} more skills)")
        return lines

    def load(self, name: str) -> str:
        skills = self._scan()
        try:
            skill = skills[name]
        except KeyError as exc:
            raise KeyError(name) from exc
        if not self._is_allowed_path(skill.path):
            raise OSError(f"skill path is outside the allowed roots: {skill.path}")
        text, truncated = _read_skill_text(skill.path)
        body = _body(text)
        if text.startswith("---") and not body:
            # Unclosed or empty frontmatter: do not fall back to the raw file,
            # which could turn body lines into apparent metadata.
            return ""
        body = body.strip() or text.strip()
        return _bound_body(body, truncated)

    def _is_allowed_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return False
        for root, _source in self._roots:
            if root.is_symlink():
                continue
            try:
                root_resolved = root.resolve()
            except (OSError, RuntimeError):
                continue
            if resolved.is_relative_to(root_resolved):
                return True
        return False

    def _scan(self) -> dict[str, Skill]:
        if self._skills is not None:
            return self._skills
        found: dict[str, Skill] = {}
        for root, source in self._roots:
            if len(found) >= MAX_SKILLS:
                logger.warning("skill limit reached (%d); ignoring remaining roots", MAX_SKILLS)
                break
            if root.is_symlink():
                logger.warning("skipping symlink skill root: %s", root)
                continue
            if not root.is_dir():
                continue
            try:
                candidates = sorted(root.glob("*/SKILL.md"))
                root_resolved = root.resolve()
            except (OSError, RuntimeError) as exc:
                logger.warning("could not scan skills under %s: %s", root, exc)
                continue
            for path in candidates:
                if len(found) >= MAX_SKILLS:
                    logger.warning("skill limit reached (%d)", MAX_SKILLS)
                    break
                if path.is_symlink() or path.parent.is_symlink():
                    logger.warning("skipping symlink skill: %s", path)
                    continue
                try:
                    resolved = path.resolve()
                except (OSError, RuntimeError):
                    logger.warning("skipping unresolvable skill: %s", path)
                    continue
                if not resolved.is_relative_to(root_resolved) or not path.is_file():
                    logger.warning("skipping skill outside root or not a file: %s", path)
                    continue
                try:
                    text, truncated = _read_skill_text(resolved)
                except OSError as exc:
                    logger.warning("could not read skill %s: %s", path, exc)
                    continue
                metadata = _frontmatter(text)
                name = _clean_name(metadata.get("name") or path.parent.name)
                if not name:
                    logger.warning("skipping skill with empty name: %s", path)
                    continue
                description = _clean_description(metadata.get("description", ""))
                if truncated:
                    logger.warning(
                        "skill file truncated at %d bytes: %s",
                        MAX_SKILL_FILE_BYTES,
                        path,
                    )
                if name in found:
                    if found[name].source == source:
                        logger.warning(
                            "duplicate skill %r in %s; keeping the first",
                            name,
                            source,
                        )
                        continue
                    logger.warning(
                        "duplicate skill %r; %s overrides %s",
                        name,
                        source,
                        found[name].source,
                    )
                found[name] = Skill(
                    name=name,
                    description=description,
                    path=resolved,
                    source=source,
                )
        self._skills = found
        return found


def _read_skill_text(path: Path) -> tuple[str, bool]:
    """Read at most MAX_SKILL_FILE_BYTES + 1 bytes from a regular file."""
    if path.is_symlink():
        raise OSError(f"skill path is a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"skill is not a regular file: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(MAX_SKILL_FILE_BYTES + 1)
    finally:
        if fd != -1:
            os.close(fd)
    truncated = len(raw) > MAX_SKILL_FILE_BYTES
    text = raw[:MAX_SKILL_FILE_BYTES].decode("utf-8", errors="replace")
    return text, truncated


def _clean_name(value: str) -> str:
    return " ".join(_clean_text(value).split())[:MAX_SKILL_NAME_CHARS]


def _clean_description(value: str) -> str:
    cleaned = " ".join(_clean_text(value).split())
    if len(cleaned) > MAX_SKILL_DESCRIPTION_CHARS:
        return cleaned[:MAX_SKILL_DESCRIPTION_CHARS] + "..."
    return cleaned


def _clean_text(value: str) -> str:
    return _UNSAFE_CHARS.sub("", str(value))


def _bound_body(body: str, truncated: bool) -> str:
    marker = _TRUNCATED_MARKER
    budget = max(0, MAX_SKILL_BODY_BYTES - len(marker.encode("utf-8")))
    raw = body.encode("utf-8")
    if len(raw) > budget:
        body = raw[:budget].decode("utf-8", errors="ignore")
        truncated = True
    return body + marker if truncated else body


def _frontmatter(text: str) -> Mapping[str, str]:
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    consumed = 0
    for line in lines[1:]:
        consumed += len(line) + 1
        if consumed > MAX_FRONTMATTER_BYTES:
            return {}
        if line.strip() == "---":
            return values
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return {}


def _body(text: str) -> str:
    if not text.startswith("---"):
        return text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    consumed = 0
    for index, line in enumerate(lines[1:], start=1):
        consumed += len(line)
        if consumed > MAX_FRONTMATTER_BYTES:
            return ""
        if line.strip() == "---":
            return "".join(lines[index + 1 :])
    return ""
