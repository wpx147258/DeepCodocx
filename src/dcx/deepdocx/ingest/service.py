"""Host-side ingestion service; independent of providers and the agent loop."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ...config import AppPaths, IngestConfig
from .jobs import TERMINAL, JobStore


class IngestService:
    def __init__(self, paths: AppPaths, settings: IngestConfig) -> None:
        self.paths = paths
        self.settings = settings
        self.root = paths.data_dir / "ingest"
        self.store = JobStore(self.root / "jobs")
        self._processes: dict[str, subprocess.Popen[bytes]] = {}

    def create_job(
        self,
        library_id: str,
        library_root: Path,
        mode: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        if mode not in {"basic", "marker", "both"}:
            raise ValueError("mode must be basic, marker or both")
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        root = library_root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError("Document library directory is unavailable")
        job = self.store.create(library_id, root, mode)
        with self.store.edit(job["id"]) as job:
            job["force"] = force
            job["settings"] = {
                "basic_dpi": self.settings.basic_dpi,
                "marker_command": self.settings.marker_command,
                "marker_timeout": self.settings.marker_timeout,
                "max_upload_mb": self.settings.max_upload_mb,
            }
        return job

    def add_file(self, job_id: str, name: str, data: bytes | Path) -> dict[str, Any]:
        return self.store.add_file(
            job_id,
            name,
            data,
            max_bytes=self.settings.max_upload_mb * 1024 * 1024,
            max_files=self.settings.max_files,
        )

    def _alive(self, job: dict[str, Any]) -> bool:
        process = self._processes.get(job["id"])
        if process is not None:
            if process.poll() is None:
                return True
            self._processes.pop(job["id"], None)
            return False
        pid = job.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        # A service restart must not mistake a recycled PID for its old worker.
        proc = Path(f"/proc/{pid}/cmdline")
        if proc.is_file():
            try:
                args = proc.read_bytes().split(b"\0")
                return (
                    b"dcx.deepdocx.ingest.worker" in args
                    and os.fsencode(self.store.path(job["id"])) in args
                )
            except OSError:
                return False
        try:  # Platforms without /proc can still detect a dead process.
            os.kill(pid, 0)
            return True
        except (OSError, OverflowError):
            return False

    def get(self, job_id: str) -> dict[str, Any]:
        with self.store.edit(job_id) as job:
            if job["status"] in {"queued", "running", "indexing"} and not self._alive(job):
                job.update(
                    status="failed",
                    current=None,
                    error="The conversion worker stopped unexpectedly; start a new task to retry.",
                )
            elif job["status"] in TERMINAL:
                process = self._processes.get(job_id)
                if process is not None and process.poll() is not None:
                    self._processes.pop(job_id, None)
        return job

    def list(self) -> list[dict[str, Any]]:
        return [self.get(job["id"]) for job in self.store.list()]

    def start(self, job_id: str) -> dict[str, Any]:
        with self.store.edit(job_id) as job:
            if job.get("status") != "uploading":
                raise ValueError("This job has already started")
            files = job.get("files") or []
            if not files:
                raise ValueError("Upload at least one PDF before starting")
            if len(files) > self.settings.max_files:
                raise ValueError(f"At most {self.settings.max_files} files are allowed")
            env = self.environment()
            for mode in ("basic", "marker") if job["mode"] == "both" else (job["mode"],):
                if not env[f"{mode}_ready"]:
                    raise ValueError(env[f"{mode}_reason"])
            job_path = self.store.path(job_id)
            job.update(status="queued", error="")
            # Persist queued before spawn, then hold the short state lock until PID
            # is saved. The worker acquires this same lock before touching state.
            self.store.write(job)
            try:
                with (job_path.parent / "worker.log").open("ab") as log:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "dcx.deepdocx.ingest.worker",
                            "--job",
                            str(job_path),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        cwd=str(self.paths.cwd),
                    )
                self._processes[job_id] = process
                job["pid"] = process.pid
            except OSError as exc:
                job.update(status="failed", error=f"Could not start conversion worker: {exc}")
                self.store.write(job)
                raise
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.store.edit(job_id) as job:
            if job.get("status") not in TERMINAL:
                was_uploading = job["status"] == "uploading"
                job.update(status="cancelled", error="Cancelled by user", current=None)
                if was_uploading:
                    self.store.remove_uploads(job_id)
        # Workers check this state during converters and before publishing. Never
        # signal a persisted PID: it may belong to another process after restart.
        return job

    def wait(self, job_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            job = self.get(job_id)
            if job.get("status") in TERMINAL:
                return job
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("Ingestion job is still running")
            time.sleep(0.25)

    def environment(self) -> dict[str, Any]:
        marker = self.settings.marker_command.strip()
        marker_error = ""
        try:
            command = shlex.split(marker)
            executable = command[0] if command else ""
            marker_available = bool(executable and shutil.which(executable))
        except ValueError:
            marker_available = False
            marker_error = (
                "Marker command has invalid quoting; check deepdocx.ingest.marker_command"
            )
        missing = [name for name in ("pdftotext", "pdftoppm") if shutil.which(name) is None]
        basic_reason = "Basic requires: " + ", ".join(missing) if missing else ""
        if not marker_available and not marker_error:
            marker_error = "Marker is not available; configure deepdocx.ingest.marker_command"
        if "pdftoppm" in missing:
            marker_error = (
                marker_error + "; " if marker_error else ""
            ) + "Marker page previews require pdftoppm"
        return {
            "basic_ready": not missing,
            "marker_ready": marker_available and "pdftoppm" not in missing,
            "basic_reason": basic_reason,
            "marker_reason": marker_error,
            "basic_dpi": self.settings.basic_dpi,
            "max_upload_mb": self.settings.max_upload_mb,
            "max_files": self.settings.max_files,
        }
