"""File-backed memory records and lexical selection.

One record is one Markdown file with frontmatter. ``MEMORY.md`` is rebuilt from
the files, never appended by hand. Writes are atomic and guarded by a store lock.

Records are written only by the agent through the ``Remember`` tool. There is no
background summarizer: durable user preferences are rare, and extracting them
from every few turns produced noise instead of signal.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from ..session.lock import FileLock

logger = logging.getLogger(__name__)

MemoryType = Literal["user", "feedback", "project", "reference"]
MEMORY_TYPES: tuple[MemoryType, ...] = ("user", "feedback", "project", "reference")
_MEMORY_TYPES = set(MEMORY_TYPES)
_SLUG = re.compile(r"[^a-z0-9]+")
_SECRET = re.compile(
    r"(?i)(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*\S+|sk-[A-Za-z0-9_-]{12,}"
)


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    name: str
    description: str
    type: MemoryType
    content: str
    path: Path | None = None
    updated_at: str = ""


def looks_like_secret(text: str) -> bool:
    return _SECRET.search(text) is not None


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def index_path(self) -> Path:
        return self.root / "MEMORY.md"

    @property
    def lock_path(self) -> Path:
        return self.root / ".lock"

    def all(self) -> list[MemoryRecord]:
        if not self.root.is_dir():
            return []
        records: list[MemoryRecord] = []
        for path in sorted(self.root.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("could not read memory %s: %s", path, exc)
                continue
            metadata, body = _split_frontmatter(text)
            name = metadata.get("name") or path.stem
            memory_type = metadata.get("type", "project")
            if memory_type not in _MEMORY_TYPES:
                memory_type = "project"
            records.append(
                MemoryRecord(
                    name=name,
                    description=metadata.get("description", ""),
                    type=cast(MemoryType, memory_type),
                    content=body.strip(),
                    path=path,
                    updated_at=metadata.get("updated_at", ""),
                )
            )
        return records

    def catalog_lines(self) -> list[str]:
        return [
            f"- {record.name}: {record.description}" if record.description else f"- {record.name}"
            for record in self.all()
        ]

    def select(self, query: str, limit: int = 5) -> list[MemoryRecord]:
        tokens = [token for token in re.findall(r"[a-z0-9_]+", query.lower()) if len(token) > 1]
        records = self.all()
        if not records:
            return []
        if not tokens:
            return sorted(records, key=lambda item: item.updated_at, reverse=True)[:limit]
        scored: list[tuple[int, str, MemoryRecord]] = []
        for record in records:
            name = record.name.lower()
            description = record.description.lower()
            content = record.content.lower()
            score = 0
            for token in tokens:
                score += name.count(token) * 3
                score += description.count(token) * 2
                score += content.count(token)
            if score:
                scored.append((score, record.updated_at, record))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [record for _, _, record in scored[:limit]]

    def save(self, record: MemoryRecord) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            path = self._write_record(record)
            self._rebuild_index_unlocked()
        return path

    def _write_record(self, record: MemoryRecord) -> Path:
        slug = _slugify(record.name)
        if not slug:
            raise ValueError("memory name must contain at least one letter or digit")
        path = self.root / f"{slug}.md"
        updated = record.updated_at or datetime.now(UTC).isoformat()
        text = "\n".join(
            [
                "---",
                f"name: {json.dumps(record.name, ensure_ascii=False)}",
                f"description: {json.dumps(record.description, ensure_ascii=False)}",
                f"type: {record.type}",
                f"updated_at: {json.dumps(updated, ensure_ascii=False)}",
                "---",
                "",
                record.content.strip(),
                "",
            ]
        )
        _atomic_write(path, text.encode("utf-8"))
        return path

    def _rebuild_index_unlocked(self) -> None:
        lines = ["# Memory Index", ""]
        for record in self.all():
            file_name = record.path.name if record.path else ""
            description = f" — {record.description}" if record.description else ""
            lines.append(f"- [{record.name}]({file_name}){description}")
        _atomic_write(self.index_path, ("\n".join(lines) + "\n").encode("utf-8"))


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    metadata: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return metadata, "".join(lines[index + 1 :])
        if ":" in line:
            key, value = line.split(":", 1)
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                with contextlib.suppress(json.JSONDecodeError):
                    value = json.loads(value)
            metadata[key.strip()] = value.strip('"').strip("'")
    return {}, text


def _slugify(name: str) -> str:
    slug = _SLUG.sub("-", name.lower()).strip("-")
    return slug[:80]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
