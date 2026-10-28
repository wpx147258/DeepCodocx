from __future__ import annotations

import json
from pathlib import Path

import pytest

import cli_agent.session.store as store_module
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


def test_permission_mode_round_trip(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_permission_mode("bypass", "accept-edits")
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.permission_mode == "bypass"
        assert loaded.auto_previous == "accept-edits"
    finally:
        resumed.close()


def test_latest_permission_record_wins(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_permission_mode("bypass", "accept-edits")
    store.append_permission_mode("default", None)
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.permission_mode == "default"
        assert loaded.auto_previous is None
    finally:
        resumed.close()


def test_corrupt_usage_record_is_skipped(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_message(user_message("kept"))
    with store.jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"v":1,"type":"usage","model":"mock-1",'
            '"input_tokens":"not-a-number","output_tokens":1}\n'
        )
    store.append_usage(Usage(2, 3), "mock-1")
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.messages[0].text() == "kept"
        assert loaded.usage == Usage(2, 3)
        assert loaded.degraded
    finally:
        resumed.close()


@pytest.mark.parametrize(
    "raw",
    ['"x"', "null", "[]", "{}", "NaN", "Infinity", "1e400", "3.9", "true"],
)
def test_corrupt_usage_values_are_skipped(tmp_path: Path, raw: str) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_message(user_message("kept"))
    with store.jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"v":1,"type":"usage","model":"mock-1","input_tokens":'
            + raw
            + ',"output_tokens":1}\n'
        )
    store.append_usage(Usage(2, 3), "mock-1")
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.messages[0].text() == "kept"
        assert loaded.usage == Usage(2, 3)
        assert loaded.degraded
    finally:
        resumed.close()


def test_valid_usage_before_corrupt_is_kept(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_usage(Usage(1, 2), "mock-1")
    with store.jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"v":1,"type":"usage","input_tokens":"bad","output_tokens":1}\n'
        )
    store.append_usage(Usage(3, 4), "mock-1")
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.usage == Usage(4, 6)
        assert loaded.degraded
    finally:
        resumed.close()


def test_corrupt_session_identity_is_ignored(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    with store.jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"v": 1, "type": "session", "provider": {"x": 1}, "model": ["m"]}
            )
            + "\n"
        )
    store.append_message(user_message("kept"))
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.provider == "mock"
        assert loaded.model == "mock-1"
        assert loaded.messages[0].text() == "kept"
        assert loaded.degraded
    finally:
        resumed.close()


def test_corrupt_message_payload_is_skipped(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_message(user_message("kept"))
    with store.jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write('{"v":1,"type":"message","message":null}\n')
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.messages[0].text() == "kept"
        assert loaded.degraded
    finally:
        resumed.close()


def test_oversized_json_integer_is_skipped(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_message(user_message("kept"))
    with store.jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"v":1,"type":"usage","input_tokens":' + "9" * 5_000 + "}\n"
        )
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.messages[0].text() == "kept"
        assert loaded.usage == Usage()
        assert loaded.degraded
    finally:
        resumed.close()


def test_resume_unreadable_jsonl_releases_lock(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_message(user_message("kept"))
    session_id = store.session_id
    store.close()

    original = store.jsonl_path.read_bytes()
    store.jsonl_path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(UnicodeDecodeError):
        SessionStore.resume(paths, session_id)

    store.jsonl_path.write_bytes(original)
    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.messages[0].text() == "kept"
    finally:
        resumed.close()


def test_resume_non_persistent_does_not_delete_session(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_message(user_message("kept"))
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id, persist=False)
    resumed.close()
    assert loaded.messages[0].text() == "kept"
    assert store.jsonl_path.exists()


def test_store_close_is_idempotent(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    session_id = store.session_id
    store.close()
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.messages == ()
    finally:
        resumed.close()


def test_clear_resets_messages_and_checkpoint_but_keeps_usage(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_message(user_message("before clear"))
    store.append_usage(Usage(1, 2), "mock-1")
    store.save_context({"summary": "old compaction"})
    store.append_clear()
    store.save_context(None)
    store.append_usage(Usage(3, 4), "mock-1")
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.usage == Usage(4, 6)
        assert loaded.messages == ()
        assert loaded.checkpoint is None
    finally:
        resumed.close()


def test_clear_as_last_record_keeps_usage(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_message(user_message("before clear"))
    store.append_usage(Usage(9, 9), "mock-1")
    store.append_clear()
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.usage == Usage(9, 9)
        assert loaded.messages == ()
    finally:
        resumed.close()


def test_legacy_clear_record_keeps_usage(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_usage(Usage(5, 6), "mock-1")
    with store.jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write('{"v":1,"type":"clear","at":"2026-01-01T00:00:00+00:00"}\n')
    session_id = store.session_id
    store.close()

    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.usage == Usage(5, 6)
    finally:
        resumed.close()


def test_append_message_wraps_oserror_as_session_error(
    tmp_path: Path, monkeypatch
) -> None:
    paths = _paths(tmp_path)
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    try:
        def fail(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(store_module, "append_line", fail)
        with pytest.raises(SessionError):
            store.append_message(user_message("hello"))
    finally:
        store.close()


def test_new_session_wraps_header_append_failure(tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store_module, "append_line", fail)
    with pytest.raises(SessionError):
        SessionStore.new(
            paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
        )
