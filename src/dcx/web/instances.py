"""Discover local web instances and choose a free port for a workspace.

Each server records a small JSON file under the XDG data directory. The file
lets a later launcher tell whether a port belongs to another workspace (try the
next port) or to the same workspace (report the running server instead of
starting a duplicate). A missing or stale record is harmless: the TCP probe
still prevents binding over an unrelated listener.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..atomic import atomic_write_text

INSTANCE_VERSION = 1
PORT_PROBE_TIMEOUT = 0.2


@dataclass(frozen=True, slots=True)
class WebInstance:
    pid: int
    host: str
    port: int
    cwd: str
    started_at: str = ""
    path: Path | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


@dataclass(frozen=True, slots=True)
class PortChoice:
    """A selected port, plus the same-workspace instance when one exists."""

    port: int
    existing: WebInstance | None = None


def instances_dir(data_dir: Path) -> Path:
    return data_dir / "web" / "instances"


def _instance_path(data_dir: Path, pid: int, port: int) -> Path:
    return instances_dir(data_dir) / f"{pid}-{port}.json"


def write_instance(
    data_dir: Path,
    *,
    host: str,
    port: int,
    cwd: str,
) -> Path:
    """Record this process as the owner of ``port`` before it starts serving."""
    directory = instances_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = _instance_path(data_dir, os.getpid(), port)
    payload: dict[str, Any] = {
        "v": INSTANCE_VERSION,
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "cwd": cwd,
        "started_at": datetime.now(UTC).isoformat(),
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False))
    return path


def remove_instance(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Probe a local TCP port without depending on ss or lsof."""
    target = _probe_host(host)
    try:
        with socket.create_connection((target, port), timeout=PORT_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def _probe_host(host: str) -> str:
    if host in {"", "0.0.0.0"}:
        return "127.0.0.1"
    if host in {"::", "[::]"}:
        return "::1"
    return host


def _pid_alive(pid: int) -> bool | None:
    """Best-effort liveness check; ``None`` means the platform cannot tell."""
    if pid <= 0:
        return False
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _read_instance(path: Path) -> WebInstance | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    pid = raw.get("pid")
    port = raw.get("port")
    cwd = raw.get("cwd")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not isinstance(cwd, str)
        or not cwd
    ):
        return None
    if not 1 <= port <= 65_535:
        return None
    host = raw.get("host")
    started = raw.get("started_at")
    return WebInstance(
        pid=pid,
        host=host if isinstance(host, str) and host else "127.0.0.1",
        port=port,
        cwd=cwd,
        started_at=started if isinstance(started, str) else "",
        path=path,
    )


def active_instances(data_dir: Path) -> list[WebInstance]:
    """Return live recorded instances and drop records that are clearly stale."""
    directory = instances_dir(data_dir)
    if not directory.is_dir():
        return []
    active: list[WebInstance] = []
    for path in directory.glob("*.json"):
        instance = _read_instance(path)
        if instance is None:
            remove_instance(path)
            continue
        alive = _pid_alive(instance.pid)
        running = alive if alive is not None else port_in_use(instance.port, instance.host)
        if not running:
            remove_instance(path)
            continue
        active.append(instance)
    return active


def select_port(
    data_dir: Path,
    start: int,
    cwd: Path,
    *,
    host: str = "127.0.0.1",
) -> PortChoice:
    """Choose a port, or report the instance already serving ``cwd``."""
    if not 1 <= start <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    resolved = cwd.resolve()
    active = active_instances(data_dir)
    for instance in active:
        try:
            same = Path(instance.cwd).resolve() == resolved
        except (OSError, RuntimeError):
            same = False
        if same:
            return PortChoice(port=instance.port, existing=instance)

    occupied = {instance.port for instance in active}
    port = start
    while port <= 65_535:
        if port not in occupied and not port_in_use(port, host):
            return PortChoice(port=port)
        port += 1
    raise ValueError("No free TCP port is available")
