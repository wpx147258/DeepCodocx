"""Atomic file primitives shared by sessions, spills, and memory."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def append_line(path: Path, line: str, *, mode: int = 0o600) -> None:
    """Append one UTF-8 line and fsync it.

    Callers must hold an exclusive writer lock: ``O_APPEND`` makes a single
    ``os.write`` atomic, but finishing a short write needs several calls. On
    failure the file is truncated back to its pre-call size, so a partial line
    is never left for the next record to merge into.
    """
    if "\n" in line:
        raise ValueError("append_line takes a single line without newlines")
    data = (line + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, mode)
    try:
        start = os.fstat(fd).st_size
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0 or written > len(view):
                    raise OSError("short write while appending line")
                view = view[written:]
            os.fsync(fd)
        except OSError:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, start)
            raise
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
