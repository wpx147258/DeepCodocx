from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

import dcx.atomic as atomic


def test_append_line_handles_short_writes(tmp_path: Path, monkeypatch) -> None:
    real_write = os.write
    calls: list[bytes] = []

    def short_write(fd: int, data: bytes) -> int:
        chunk = bytes(data)[:3]
        calls.append(chunk)
        return real_write(fd, chunk)

    monkeypatch.setattr(atomic.os, "write", short_write)
    atomic.append_line(tmp_path / "session.jsonl", "hello")
    assert (tmp_path / "session.jsonl").read_text(encoding="utf-8") == "hello\n"
    assert len(calls) > 1
    assert b"".join(calls) == b"hello\n"


def test_append_line_rejects_zero_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(atomic.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError):
        atomic.append_line(tmp_path / "session.jsonl", "hello")


def test_append_line_rejects_negative_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(atomic.os, "write", lambda fd, data: -1)
    with pytest.raises(OSError):
        atomic.append_line(tmp_path / "session.jsonl", "hello")


def test_append_line_rolls_back_partial_write_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "session.jsonl"
    real_write = os.write
    calls = 0

    def failing_write(fd: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, bytes(data)[:3])
        raise OSError(errno.EIO, "disk error")

    monkeypatch.setattr(atomic.os, "write", failing_write)
    with pytest.raises(OSError):
        atomic.append_line(path, '{"first": true}')

    assert path.read_bytes() == b""
    monkeypatch.undo()
    atomic.append_line(path, "next")
    assert path.read_text(encoding="utf-8") == "next\n"


def test_append_line_preserves_existing_content(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text("a\n", encoding="utf-8")
    atomic.append_line(path, "b")
    assert path.read_text(encoding="utf-8") == "a\nb\n"


def test_append_line_writes_utf8(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    atomic.append_line(path, "会话")
    assert path.read_bytes() == "会话\n".encode()


def test_append_line_rejects_embedded_newline(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        atomic.append_line(tmp_path / "session.jsonl", "one\ntwo")
    assert not (tmp_path / "session.jsonl").exists()
