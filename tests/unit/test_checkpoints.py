from __future__ import annotations

import stat
from pathlib import Path

from cli_agent.session.checkpoints import CheckpointStore
from cli_agent.session.paths import SessionPaths


def _paths(tmp_path: Path) -> SessionPaths:
    root = tmp_path / "session"
    return SessionPaths(
        root=root,
        jsonl=root / "session.jsonl",
        context=root / "context.json",
        meta=root / "meta.json",
        lock=root / "lock",
        spills=root / "spills",
        archive=root / "archive",
        checkpoints=root / "checkpoints",
    )


def _store(tmp_path: Path, **kwargs) -> CheckpointStore:
    return CheckpointStore(_paths(tmp_path), tmp_path, **kwargs)


def test_restore_modified_file(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("before", encoding="utf-8")
    store = _store(tmp_path)
    store.begin("msg_1")
    store.capture("a.txt")
    path.write_text("after", encoding="utf-8")
    store.record_after("a.txt")

    preview = store.preview(["msg_1"])
    assert preview.has_changes
    assert preview.files[0].status == "modified"
    assert preview.files[0].conflict is False

    result = store.restore(["msg_1"])
    assert result.restored == ("a.txt",)
    assert path.read_text(encoding="utf-8") == "before"


def test_created_file_is_deleted_on_restore(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.begin("msg_1")
    store.capture("new.txt")
    (tmp_path / "new.txt").write_text("made", encoding="utf-8")
    store.record_after("new.txt")

    preview = store.preview(["msg_1"])
    assert preview.files[0].status == "created"
    assert store.restore(["msg_1"]).restored == ("new.txt",)
    assert not (tmp_path / "new.txt").exists()


def test_conflict_requires_force(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("before", encoding="utf-8")
    store = _store(tmp_path)
    store.begin("msg_1")
    store.capture("a.txt")
    path.write_text("agent", encoding="utf-8")
    store.record_after("a.txt")
    path.write_text("user", encoding="utf-8")

    assert store.preview(["msg_1"]).files[0].conflict is True
    blocked = store.restore(["msg_1"])
    assert blocked.aborted and blocked.conflicts == ("a.txt",)
    assert path.read_text(encoding="utf-8") == "user"

    forced = store.restore(["msg_1"], force=True)
    assert forced.restored == ("a.txt",)
    assert path.read_text(encoding="utf-8") == "before"


def test_multiple_turns_restore_in_reverse_order(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("v0", encoding="utf-8")
    store = _store(tmp_path)

    store.begin("msg_1")
    store.capture("a.txt")
    path.write_text("v1", encoding="utf-8")
    store.record_after("a.txt")

    store.begin("msg_2")
    store.capture("a.txt")
    path.write_text("v2", encoding="utf-8")
    store.record_after("a.txt")
    store.capture("b.txt")
    (tmp_path / "b.txt").write_text("created", encoding="utf-8")
    store.record_after("b.txt")

    preview = store.preview(["msg_1", "msg_2"])
    assert {item.path for item in preview.files} == {"a.txt", "b.txt"}
    result = store.restore(["msg_1", "msg_2"])
    assert set(result.restored) == {"a.txt", "b.txt"}
    assert path.read_text(encoding="utf-8") == "v0"
    assert not (tmp_path / "b.txt").exists()


def test_shell_only_checkpoint_is_reported(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.begin("msg_1")
    store.note_shell("touch generated.txt")
    preview = store.preview(["msg_1"])
    assert preview.shell_ran
    assert not preview.has_changes
    assert "touch generated.txt" in preview.commands


def test_outside_workspace_is_not_checkpointed(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    store = _store(tmp_path / "workspace")
    store.begin("msg_1")
    store.capture(str(outside))
    preview = store.preview(["msg_1"])
    assert not preview.has_changes
    assert any("outside the workspace" in item for item in preview.warnings)


def test_symlink_is_not_checkpointed(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    store = _store(tmp_path)
    store.begin("msg_1")
    store.capture("link.txt")
    preview = store.preview(["msg_1"])
    assert not preview.has_changes
    assert any("not checkpointed" in item for item in preview.warnings)


def test_missing_blob_aborts_restore(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("before", encoding="utf-8")
    store = _store(tmp_path)
    store.begin("msg_1")
    store.capture("a.txt")
    path.write_text("after", encoding="utf-8")
    store.record_after("a.txt")
    for blob in (tmp_path / "session" / "checkpoints" / "blobs").glob("*/*"):
        blob.unlink()
    result = store.restore(["msg_1"])
    assert result.aborted
    assert path.read_text(encoding="utf-8") == "after"


def test_oversized_file_marks_the_checkpoint_partial(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("too large", encoding="utf-8")
    store = _store(tmp_path, max_file_bytes=3)
    store.begin("msg_1")
    store.capture("big.txt")
    preview = store.preview(["msg_1"])
    assert preview.partial is True
    assert preview.restorable is False
    assert (tmp_path / "big.txt").read_text(encoding="utf-8") == "too large"
    assert store.restore(["msg_1"]).aborted is True


def test_old_checkpoints_are_pruned(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    store = _store(tmp_path, max_checkpoints=1)
    for turn, value in (("msg_1", "one"), ("msg_2", "two")):
        path.write_text(value, encoding="utf-8")
        store.begin(turn)
        store.capture("a.txt")
        path.write_text(value + "-changed", encoding="utf-8")
        store.record_after("a.txt")

    store.begin("msg_3")
    assert not store.preview(["msg_1"]).has_changes
    assert store.preview(["msg_2"]).has_changes


def test_reentering_the_same_turn_keeps_its_checkpoint(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    store = _store(tmp_path)
    store.begin("msg_1")
    store.capture("a.txt")
    a.write_text("a1", encoding="utf-8")
    store.record_after("a.txt")

    # A continue/regenerate re-enters the same user message id.
    store.begin("msg_1")
    store.capture("b.txt")
    b.write_text("b1", encoding="utf-8")
    store.record_after("b.txt")

    preview = store.preview(["msg_1"])
    assert {item.path for item in preview.files} == {"a.txt", "b.txt"}
    result = store.restore(["msg_1"])
    assert set(result.restored) == {"a.txt", "b.txt"}
    assert not a.exists() and not b.exists()


def test_restore_does_not_follow_a_symlinked_parent(tmp_path: Path) -> None:
    inside = tmp_path / "d"
    inside.mkdir()
    path = inside / "f.txt"
    path.write_text("before", encoding="utf-8")
    store = _store(tmp_path)
    store.begin("msg_1")
    store.capture("d/f.txt")
    path.write_text("agent", encoding="utf-8")
    store.record_after("d/f.txt")

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    (outside / "f.txt").write_text("external", encoding="utf-8")
    inside.rename(tmp_path / "d-real")
    (tmp_path / "d").symlink_to(outside, target_is_directory=True)

    result = store.restore(["msg_1"], force=True)
    assert "d/f.txt" in result.skipped
    assert result.aborted
    assert (outside / "f.txt").read_text(encoding="utf-8") == "external"


def test_created_file_is_not_deleted_through_a_symlinked_parent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.begin("msg_1")
    store.capture("d/new.txt")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "new.txt").write_text("agent", encoding="utf-8")
    store.record_after("d/new.txt")

    outside = tmp_path.parent / f"{tmp_path.name}-outside2"
    outside.mkdir(exist_ok=True)
    (outside / "new.txt").write_text("external", encoding="utf-8")
    (tmp_path / "d").rename(tmp_path / "d-real")
    (tmp_path / "d").symlink_to(outside, target_is_directory=True)

    result = store.restore(["msg_1"], force=True)
    assert "d/new.txt" in result.skipped
    assert result.aborted
    assert (outside / "new.txt").exists()


def test_corrupt_manifest_does_not_break_begins(tmp_path: Path) -> None:
    store = _store(tmp_path)
    turns = tmp_path / "session" / "checkpoints" / "turns"
    turns.mkdir(parents=True)
    (turns / "msg_bad.json").write_bytes(b"\xff\xfe")
    (turns / "msg_null.json").write_text('{"files": null}', encoding="utf-8")

    store.begin("msg_1")
    store.capture("a.txt")
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    store.record_after("a.txt")
    assert store.preview(["msg_1"]).has_changes


def test_workspace_mismatch_is_surfaced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.begin("msg_1")
    (tmp_path / "a.txt").write_text("before", encoding="utf-8")
    store.capture("a.txt")
    (tmp_path / "a.txt").write_text("after", encoding="utf-8")
    store.record_after("a.txt")

    other = CheckpointStore(
        _paths(tmp_path), tmp_path / "different-workspace"
    )
    preview = other.preview(["msg_1"])
    assert any("different workspace" in warning for warning in preview.warnings)
    assert other.restore(["msg_1"]).aborted


def test_corrupt_blob_aborts_restore(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("before", encoding="utf-8")
    store = _store(tmp_path)
    store.begin("msg_1")
    store.capture("a.txt")
    path.write_text("after", encoding="utf-8")
    store.record_after("a.txt")

    blob = next((tmp_path / "session" / "checkpoints" / "blobs").glob("*/*"))
    blob.write_bytes(b"corrupt")
    preview = store.preview(["msg_1"])
    assert preview.restorable is False
    result = store.restore(["msg_1"], force=True)
    assert result.aborted
    assert path.read_text(encoding="utf-8") == "after"


def test_discard_removes_orphan_manifests(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.begin("msg_1")
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    store.capture("a.txt")
    store.discard(["msg_1"])
    assert not store.preview(["msg_1"]).has_changes
    assert not (tmp_path / "session" / "checkpoints" / "turns" / "msg_1.json").exists()


def test_hardlinked_file_is_not_checkpointed(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.hardlink_to(target)
    store = _store(tmp_path)
    store.begin("msg_1")
    store.capture("link.txt")
    preview = store.preview(["msg_1"])
    assert not preview.has_changes
    assert any("hard-linked" in warning for warning in preview.warnings)


def test_mode_change_is_restored(tmp_path: Path) -> None:
    path = tmp_path / "a.sh"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o640)
    store = _store(tmp_path)
    store.begin("msg_1")
    store.capture("a.sh")
    path.chmod(0o600)
    store.record_after("a.sh")
    assert store.preview(["msg_1"]).files[0].status == "modified"
    assert store.restore(["msg_1"]).restored == ("a.sh",)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
