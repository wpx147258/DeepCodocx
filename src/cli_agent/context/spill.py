"""Spill large tool output to disk and return a compact pointer."""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


class SpillStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def spill(self, tool_use_id: str, text: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        safe = _SAFE_ID.sub("_", tool_use_id)[:120] or "tool"
        path = (self.root / f"{safe}.txt").resolve()
        if self.root.resolve() not in path.parents:
            raise RuntimeError("spill path escaped the session directory")
        if not path.exists():
            _atomic_write(path, text.encode("utf-8"))
        return path

    def pointer(self, text: str, path: Path, preview_tokens: int = 1_500) -> str:
        preview_chars = max(400, preview_tokens * 4)
        head = text[: preview_chars * 2 // 3]
        tail = text[-preview_chars // 3 :] if len(text) > preview_chars else ""
        parts = [
            f"[tool output spilled: {path}",
            f" bytes={len(text.encode('utf-8'))}",
            " preview:",
            head,
        ]
        if tail:
            parts.extend(["", "...", tail])
        parts.append(
            "Use Read with offset/limit to inspect the full file.]"
        )
        return "\n".join(parts)


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
