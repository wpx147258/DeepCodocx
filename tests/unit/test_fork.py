from __future__ import annotations

import json
from pathlib import Path

import pytest

from dcx.config import AppPaths
from dcx.errors import SessionError
from dcx.messages import Usage, user_message
from dcx.session.fork import fork_session
from dcx.session.lock import FileLock
from dcx.session.store import SessionStore


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths.discover(
        tmp_path,
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
    )


def _source_session(paths: AppPaths, tmp_path: Path) -> str:
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_message(user_message("hello"))
    store.append_usage(Usage(10, 2, 3, 0), "mock-1")
    store.save_context(
        {
            "summary": "s",
            "authoritative_request": "hello",
            "keep_from_message_id": "msg_x",
            "archive_path": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    store.spills_dir.mkdir(parents=True, exist_ok=True)
    (store.spills_dir / "t1.txt").write_text("big output", encoding="utf-8")
    session_id = store.session_id
    store.close()
    return session_id


def _records(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_fork_copies_history_and_metadata(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    source_id = _source_session(paths, tmp_path)

    new_id = fork_session(paths, source_id)

    assert new_id != source_id
    target = paths.session_dir(new_id)
    assert (target / "session.jsonl").is_file()
    assert (target / "context.json").is_file()
    assert (target / "spills" / "t1.txt").read_text(encoding="utf-8") == "big output"
    assert not (target / "lock").exists()

    meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
    assert meta["session_id"] == new_id
    assert meta["forked_from"] == source_id

    first = _records(target / "session.jsonl")[0]
    assert first["type"] == "session"
    assert first["id"] == new_id
    assert first["forked_from"] == source_id

    # The source is untouched apart from its lock file.
    source = paths.session_dir(source_id)
    source_meta = json.loads((source / "meta.json").read_text(encoding="utf-8"))
    assert source_meta["session_id"] == source_id
    assert "forked_from" not in source_meta
    assert _records(source / "session.jsonl")[0]["id"] == source_id


def test_fork_works_while_the_source_is_locked(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    source_id = _source_session(paths, tmp_path)

    lock = FileLock(paths.session_dir(source_id) / "lock")
    lock.acquire()
    try:
        new_id = fork_session(paths, source_id)
    finally:
        lock.release()

    assert (paths.session_dir(new_id) / "session.jsonl").is_file()


def test_fork_unknown_session_is_an_error(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    with pytest.raises(SessionError, match="unknown session"):
        fork_session(paths, "sess_missing")


def test_fork_rejects_invalid_ids(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    with pytest.raises(SessionError):
        fork_session(paths, "../escape")
