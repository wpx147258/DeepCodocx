from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent.config import AppPaths
from cli_agent.errors import SessionError
from cli_agent.messages import Usage, user_message
from cli_agent.session.paths import list_session_ids
from cli_agent.session.store import SessionStore


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths.discover(
        tmp_path,
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
    )


def test_round_trip_with_usage_and_checkpoint(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    message = user_message("hello")
    store.append_message(message)
    store.append_usage(Usage(3, 4, 5, 6), "mock-1")
    store.save_context(
        {
            "summary": "s",
            "authoritative_request": "hello",
            "keep_from_message_id": message.id,
            "archive_path": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.messages[0].text() == "hello"
        assert loaded.usage == Usage(3, 4, 5, 6)
        assert loaded.provider == "mock"
        assert loaded.model == "mock-1"
        assert loaded.checkpoint is not None
        assert loaded.checkpoint["summary"] == "s"
        assert not loaded.degraded
    finally:
        resumed.close()


def test_torn_final_line_is_ignored(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_message(user_message("kept"))
    with store.jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write('{"v":1,"type":"message","message":{')
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert len(loaded.messages) == 1
        assert not loaded.degraded
    finally:
        resumed.close()


def test_corrupt_middle_line_marks_degraded(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    with store.jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    store.append_message(user_message("after corruption"))
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.degraded
        assert loaded.messages[0].text() == "after corruption"
    finally:
        resumed.close()


def test_second_writer_fails_closed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    try:
        with pytest.raises(SessionError):
            SessionStore.resume(paths, first.session_id)
    finally:
        first.close()


def test_ephemeral_store_cleans_up(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=False
    )
    root = store.paths.root
    store.append_message(user_message("not persisted"))
    store.close()
    assert not root.exists()


def test_list_sessions_newest_first(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    first_id = first.session_id
    first.close()
    second = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    second_id = second.session_id
    second.close()
    ids = list_session_ids(paths)
    assert ids[0] == second_id
    assert first_id in ids
