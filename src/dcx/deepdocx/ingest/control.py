"""Cancellation and short cross-process locks for host-side ingestion."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ...errors import SessionError
from ...session.lock import FileLock


class IngestCancelled(RuntimeError):
    pass


@contextmanager
def locked(
    path: Path,
    cancelled: Callable[[], bool] | None = None,
    *,
    timeout: float | None = 10,
) -> Iterator[None]:
    lock = FileLock(path)
    started = time.monotonic()
    while True:
        if cancelled and cancelled():
            raise IngestCancelled("Cancelled by user")
        try:
            lock.acquire()
            break
        except SessionError as exc:
            if timeout is not None and time.monotonic() - started >= timeout:
                raise ValueError(
                    "Another ingestion operation holds this lock; retry shortly"
                ) from exc
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.release()
