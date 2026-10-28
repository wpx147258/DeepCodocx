"""Mutable per-session display metadata (title and archive flag).

The append-only session log is not the right place for UI-only state: renaming a
chat should not rewrite history or invalidate the prompt cache. This module keeps
that state in ``meta.json`` with the same atomic-write discipline as the rest of
the session store.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..atomic import atomic_write_text
from .paths import SessionPaths

logger = logging.getLogger(__name__)
MAX_TITLE_CHARS = 120


def read_session_meta(paths: SessionPaths) -> dict[str, Any]:
    if not paths.meta.is_file():
        return {}
    try:
        data = json.loads(paths.meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def clean_title(value: object) -> str | None:
    """Normalize a user-supplied title; ``None`` when nothing usable remains."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    text = "".join(char for char in text if char.isprintable()).strip()
    if not text:
        return None
    return text[:MAX_TITLE_CHARS]


def update_session_meta(
    paths: SessionPaths,
    *,
    title: str | None = None,
    archived: bool | None = None,
) -> None:
    meta = read_session_meta(paths)
    meta.setdefault("v", 1)
    meta.setdefault("session_id", paths.root.name)
    if title is not None:
        cleaned = clean_title(title)
        if cleaned is None:
            raise ValueError("title must not be empty")
        meta["title"] = cleaned
    if archived is not None:
        meta["archived"] = bool(archived)
    atomic_write_text(paths.meta, json.dumps(meta, ensure_ascii=False, indent=2))
