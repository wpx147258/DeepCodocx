"""Session path layout and identifiers."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..config import AppPaths
from ..errors import SessionError

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:12]}"


def validate_session_id(session_id: str) -> str:
    if not _SESSION_ID.match(session_id) or ".." in session_id:
        raise SessionError(f"invalid session id: {session_id!r}")
    return session_id


@dataclass(frozen=True, slots=True)
class SessionPaths:
    root: Path
    jsonl: Path
    context: Path
    meta: Path
    lock: Path
    spills: Path
    archive: Path

    @classmethod
    def for_session(cls, paths: AppPaths, session_id: str) -> SessionPaths:
        validate_session_id(session_id)
        root = paths.session_dir(session_id)
        return cls(
            root=root,
            jsonl=root / "session.jsonl",
            context=root / "context.json",
            meta=root / "meta.json",
            lock=root / "lock",
            spills=root / "spills",
            archive=root / "archive",
        )


def list_session_ids(paths: AppPaths) -> list[str]:
    sessions = paths.sessions_dir
    if not sessions.is_dir():
        return []
    entries: list[tuple[float, str]] = []
    for directory in sessions.iterdir():
        if not directory.is_dir():
            continue
        jsonl = directory / "session.jsonl"
        if not jsonl.is_file():
            continue
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        entries.append((mtime, directory.name))
    entries.sort(reverse=True)
    return [session_id for _, session_id in entries]


def latest_session_id(paths: AppPaths) -> str | None:
    ids = list_session_ids(paths)
    return ids[0] if ids else None
