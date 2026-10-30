"""Run a fixed converter command with bounded waits and cooperative cancellation."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from .control import IngestCancelled


def run_command(
    command: list[str],
    *,
    timeout: int = 300,
    cwd: Path | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    import time

    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    ) as process:
        try:
            while True:
                if cancelled and cancelled():
                    raise IngestCancelled("Cancelled by user")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{command[0]} timed out after {timeout}s")
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            if process.poll() is None:
                if os.name == "posix":
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover - Windows
                    process.kill()
            process.communicate()
            raise
        if process.returncode:
            raise RuntimeError(f"{command[0]} failed: {(stderr or stdout)[-1200:]}")
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
