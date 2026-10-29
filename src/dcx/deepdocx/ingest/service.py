"""Host-side ingestion service: create jobs, upload files, start/cancel workers."""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import signal
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
        root = library_root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError("Document library directory is unavailable")
        job = self.store.create(library_id, root, mode)
        job["force"] = bool(force)
        job["settings"] = {
            "basic_dpi": self.settings.basic_dpi,
            "marker_command": self.settings.marker_command,
            "marker_timeout": self.settings.marker_timeout,
            "max_upload_mb": self.settings.max_upload_mb,
        }
        self.store.write(job)
        return job

    def add_file(self, job_id: str, name: str, data: bytes) -> dict[str, Any]:
        return self.store.add_file(
            job_id,
            name,
            data,
            max_bytes=self.settings.max_upload_mb * 1024 * 1024,
        )

    def get(self, job_id: str) -> dict[str, Any]:
        return self.store.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        return self.store.list()

    def start(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job.get("status") not in {"uploading", "queued"}:
            raise ValueError("This job has already started")
        files = job.get("files") or []
        if not files:
            raise ValueError("Upload at least one PDF before starting")
        if len(files) > self.settings.max_files:
            raise ValueError(f"At most {self.settings.max_files} files are allowed")
        if job.get("mode") in {"marker", "both"} and not self.environment()["marker_ready"]:
            raise ValueError("Marker is not available; configure deepdocx.ingest.marker_command")
        job["status"] = "queued"
        self.store.write(job)
        job_path = self.store.path(job_id)
        log_path = job_path.parent / "worker.log"
        log = log_path.open("ab")
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "dcx.deepdocx.ingest.worker", "--job", str(job_path)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(self.paths.cwd),
            )
        finally:
            log.close()
        self._processes[job_id] = process
        job["pid"] = process.pid
        self.store.write(job)
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job.get("status") in TERMINAL:
            return job
        process = self._processes.pop(job_id, None)
        pid = process.pid if process is not None else job.get("pid")
        if isinstance(pid, int):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        job["status"] = "cancelled"
        job["error"] = "Cancelled by user"
        self.store.write(job)
        return job

    def wait(self, job_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            job = self.store.get(job_id)
            if job.get("status") in TERMINAL:
                return job
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("Ingestion job is still running")
            time.sleep(0.25)

    def environment(self) -> dict[str, Any]:
        marker = self.settings.marker_command.strip()
        marker_bin = shlex.split(marker)[0] if marker else ""
        marker_ready = bool(
            marker_bin
            and (
                Path(marker_bin).is_file()
                if os.path.isabs(marker_bin)
                else shutil.which(marker_bin) is not None
            )
        )
        basic_ready = shutil.which("pdftotext") is not None and shutil.which("pdftoppm") is not None
        return {
            "basic_ready": basic_ready,
            "marker_ready": marker_ready,
            "marker_command": marker,
            "basic_dpi": self.settings.basic_dpi,
            "max_upload_mb": self.settings.max_upload_mb,
            "max_files": self.settings.max_files,
        }
