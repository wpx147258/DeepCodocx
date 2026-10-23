"""File-backed memory: store, lexical selection, extraction, coalescing.

One record is one Markdown file. ``MEMORY.md`` is rebuilt from files, never
appended by hand. Writes are atomic and guarded by a store lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from ..messages import Message
from ..session.lock import FileLock

logger = logging.getLogger(__name__)

MemoryType = Literal["user", "feedback", "project", "reference"]
_MEMORY_TYPES = {"user", "feedback", "project", "reference"}
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

    def save_many(self, records: Iterable[MemoryRecord]) -> list[Path]:
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            paths = [self._write_record(record) for record in records]
            self._rebuild_index_unlocked()
        return paths

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


class MemoryExtractor:
    def __init__(
        self,
        store: MemoryStore,
        complete: Callable[[str], Awaitable[str]],
        *,
        max_conversation_chars: int = 20_000,
    ) -> None:
        self.store = store
        self.complete = complete
        self.max_conversation_chars = max_conversation_chars

    async def extract(self, messages: tuple[Message, ...]) -> list[MemoryRecord]:
        conversation = _conversation_text(messages)[- self.max_conversation_chars :]
        if not conversation.strip():
            return []
        raw = await self.complete(_EXTRACTION_PROMPT.format(conversation=conversation))
        entries = _parse_json_array(raw)
        existing = {record.name.lower() for record in self.store.all()}
        records: list[MemoryRecord] = []
        for entry in entries:
            record = _record_from_entry(entry)
            if record is None:
                continue
            if record.name.lower() in existing:
                continue
            existing.add(record.name.lower())
            records.append(record)
            if len(records) >= 5:
                break
        if records:
            self.store.save_many(records)
        return records


class ExtractionCoordinator:
    """Single-flight extraction with a dirty rerun."""

    def __init__(
        self,
        min_new_messages: int = 8,
        cooldown_seconds: float = 0.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_new_messages = max(1, min_new_messages)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self._clock = clock
        self._running = False
        self._dirty = False
        self._last_count = 0
        self._last_extraction_at: float | None = None
        self._lock = asyncio.Lock()

    async def request(
        self,
        snapshot: Callable[[], tuple[Message, ...]],
        extractor: MemoryExtractor,
    ) -> list[MemoryRecord]:
        if self._running:
            self._dirty = True
            return []
        saved: list[MemoryRecord] = []
        async with self._lock:
            self._running = True
            try:
                while True:
                    self._dirty = False
                    messages = snapshot()
                    visible = _visible_count(messages)
                    now = self._clock()
                    cooled_down = (
                        self._last_extraction_at is None
                        or now - self._last_extraction_at >= self.cooldown_seconds
                    )
                    if (
                        visible - self._last_count >= self.min_new_messages
                        and cooled_down
                    ):
                        saved.extend(await extractor.extract(messages))
                        self._last_count = visible
                        self._last_extraction_at = self._clock()
                    if not self._dirty:
                        break
            except Exception as exc:  # noqa: BLE001 - extraction is best effort
                # Background memory extraction is optional. A provider hiccup is
                # not a crash; keep one actionable line instead of a traceback.
                logger.warning("memory extraction skipped: %s", exc)
            finally:
                self._running = False
        return saved


_EXTRACTION_PROMPT = """You extract durable memories from a coding-agent conversation.
Return ONLY a JSON array. Each item must have:
  "name": short kebab-case identifier,
  "description": one line,
  "type": one of user | feedback | project | reference,
  "content": the durable fact, with why and how to apply when relevant.

Rules:
- Save only facts useful in future sessions: user preferences, project constraints,
  recurring feedback, external references.
- Do not save secrets, credentials, transient task state, or anything already obvious
  from the repository.
- Do not save the current request or plans that will be obsolete tomorrow.
- If nothing is worth saving, return [].

Conversation:
{conversation}
"""


def _visible_count(messages: tuple[Message, ...]) -> int:
    return sum(1 for message in messages if message.role in ("user", "assistant"))


def _conversation_text(messages: tuple[Message, ...]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in message.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(f"{message.role}: {text}")
            elif getattr(block, "type", "") == "tool_use":
                parts.append(f"{message.role} tool call: {getattr(block, 'name', '')}")
    return "\n".join(parts)


def _parse_json_array(raw: str) -> list[object]:
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _record_from_entry(entry: object) -> MemoryRecord | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    description = entry.get("description", "")
    memory_type = entry.get("type", "project")
    content = entry.get("content")
    if not isinstance(name, str) or not isinstance(content, str):
        return None
    if memory_type not in _MEMORY_TYPES:
        memory_type = "project"
    if not name.strip() or not content.strip():
        return None
    if _SECRET.search(content):
        return None
    return MemoryRecord(
        name=name.strip(),
        description=str(description).strip(),
        type=cast(MemoryType, memory_type),
        content=content.strip(),
    )


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
