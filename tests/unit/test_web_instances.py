"""Instance records and automatic web-port selection."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dcx.web import instances
from dcx.web.instances import (
    WebInstance,
    active_instances,
    remove_instance,
    select_port,
    write_instance,
)


def _record(path: Path, *, pid: int, port: int, cwd: Path, host: str = "127.0.0.1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "pid": pid,
                "host": host,
                "port": port,
                "cwd": str(cwd),
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def test_write_and_remove_instance(data_dir: Path, tmp_path: Path) -> None:
    path = write_instance(data_dir, host="127.0.0.1", port=8765, cwd=str(tmp_path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["port"] == 8765
    assert payload["cwd"] == str(tmp_path)
    assert path.parent == instances.instances_dir(data_dir)
    remove_instance(path)
    assert not path.exists()


def test_free_port_is_selected(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instances, "port_in_use", lambda port, host="127.0.0.1": False)
    choice = select_port(data_dir, 8765, tmp_path)
    assert choice.port == 8765
    assert choice.existing is None


def test_foreign_workspace_port_is_skipped(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(
        instances.instances_dir(data_dir) / "1001-8765.json",
        pid=1001,
        port=8765,
        cwd=tmp_path / "other",
    )
    monkeypatch.setattr(instances, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(instances, "port_in_use", lambda port, host="127.0.0.1": port == 8765)
    choice = select_port(data_dir, 8765, tmp_path)
    assert choice.port == 8766
    assert choice.existing is None


def test_unrecorded_listener_is_skipped(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instances, "port_in_use", lambda port, host="127.0.0.1": port == 8765)
    choice = select_port(data_dir, 8765, tmp_path)
    assert choice.port == 8766


def test_same_workspace_is_reported(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(
        instances.instances_dir(data_dir) / "1002-8770.json",
        pid=1002,
        port=8770,
        cwd=tmp_path,
    )
    monkeypatch.setattr(instances, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(instances, "port_in_use", lambda port, host="127.0.0.1": False)
    choice = select_port(data_dir, 8765, tmp_path)
    assert choice.port == 8770
    assert choice.existing is not None
    assert choice.existing.url == "http://127.0.0.1:8770/"


def test_stale_records_are_removed(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = instances.instances_dir(data_dir) / "1003-8765.json"
    _record(path, pid=1003, port=8765, cwd=tmp_path / "gone")
    monkeypatch.setattr(instances, "_pid_alive", lambda pid: False)
    assert active_instances(data_dir) == []
    assert not path.exists()


def test_invalid_records_are_removed(data_dir: Path) -> None:
    directory = instances.instances_dir(data_dir)
    directory.mkdir(parents=True)
    path = directory / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert active_instances(data_dir) == []
    assert not path.exists()


def test_no_free_port_is_an_error(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instances, "port_in_use", lambda port, host="127.0.0.1": True)
    with pytest.raises(ValueError, match="No free TCP port"):
        select_port(data_dir, 65_535, tmp_path)


def test_instance_url_uses_recorded_host() -> None:
    assert WebInstance(1, "0.0.0.0", 8765, "/work").url == "http://0.0.0.0:8765/"
