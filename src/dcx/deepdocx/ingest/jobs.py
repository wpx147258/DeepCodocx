"""Persistent JSON job records for document ingestion.

Jobs are deliberately outside the runtime and outside the library. They hold
uploaded source paths, mode, progress and errors; the worker mutates one job.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...atomic import atomic_write_text
from .control import locked
from .identity import doc_id_for_name, sanitize_filename, sha256_bytes

PDF_MAGIC = b"%PDF-"
TERMINAL = {"succeeded", "partial", "failed", "cancelled"}


def now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", job_id):
            raise ValueError("Invalid job id")
        return self.root / job_id

    def _path(self, job_id: str) -> Path:
        return self._dir(job_id) / "job.json"

    def path(self, job_id: str) -> Path:
        return self._path(job_id)

    def create(self, library_id: str, library_root: Path, mode: str) -> dict[str, Any]:
        if mode not in {"basic", "marker", "both"}:
            raise ValueError("mode must be basic, marker or both")
        job_id = uuid.uuid4().hex[:12]
        job: dict[str, Any] = {
            "id": job_id,
            "library_id": library_id,
            "library_root": str(library_root),
            "mode": mode,
            "status": "uploading",
            "created_at": now(),
            "updated_at": now(),
            "files": [],
            "current": None,
            "error": "",
            "warnings": [],
            "pid": None,
        }
        self._dir(job_id).mkdir(parents=True, exist_ok=False)
        self.write(job)
        return job

    def get(self, job_id: str) -> dict[str, Any]:
        path = self._path(job_id)
        if not path.is_file():
            raise ValueError("Unknown ingestion job")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Corrupt ingestion job")
        return data

    def list(self) -> list[dict[str, Any]]:
        jobs = []
        for path in self.root.glob("*/job.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("id") == path.parent.name:
                    jobs.append(data)
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(jobs, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def write(self, job: dict[str, Any]) -> None:
        job["updated_at"] = now()
        atomic_write_text(
            self._path(str(job["id"])),
            json.dumps(job, ensure_ascii=False, indent=2) + "\n",
        )

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self.edit(job_id) as job:
            job.update(changes)
        return job

    @contextmanager
    def edit(self, job_id: str) -> Iterator[dict[str, Any]]:
        # A separate lock file survives atomic replacements of job.json.
        with locked(self._dir(job_id) / "state.lock"):
            job = self.get(job_id)
            original = json.dumps(job, sort_keys=True)
            yield job
            if json.dumps(job, sort_keys=True) != original:
                self.write(job)

    def add_file(
        self,
        job_id: str,
        name: str,
        data: bytes | Path,
        *,
        max_bytes: int,
        max_files: int = 50,
    ) -> dict[str, Any]:
        with self.edit(job_id) as job:
            return self._add_file(job, name, data, max_bytes=max_bytes, max_files=max_files)

    def _add_file(
        self,
        job: dict[str, Any],
        name: str,
        data: bytes | Path,
        *,
        max_bytes: int,
        max_files: int,
    ) -> dict[str, Any]:
        size = data.stat().st_size if isinstance(data, Path) else len(data)
        if size > max_bytes:
            raise ValueError(f"File exceeds the {max_bytes // (1024 * 1024)} MiB limit")
        if isinstance(data, Path):
            with data.open("rb") as handle:
                magic = handle.read(5)
        else:
            magic = data[:5]
        if magic != PDF_MAGIC:
            raise ValueError("Only PDF uploads are supported in this version")
        safe = sanitize_filename(name)
        if not safe.lower().endswith(".pdf"):
            safe += ".pdf"
        job_id = job["id"]
        if job.get("status") != "uploading":
            raise ValueError("This job is no longer accepting uploads")
        files = job.setdefault("files", [])
        from .identity import sha256_file

        digest = sha256_file(data) if isinstance(data, Path) else sha256_bytes(data)
        if any(item.get("sha256") == digest for item in files):
            return job
        if len(files) >= max_files:
            raise ValueError(f"At most {max_files} files are allowed")
        if any(item.get("doc_id") == doc_id_for_name(safe) for item in files):
            raise ValueError("Different PDFs have the same filename; rename one before uploading")
        upload_id = f"{len(files) + 1:03d}"
        upload_dir = self._dir(job_id) / "uploads" / upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored = upload_dir / safe
        if isinstance(data, Path):
            shutil.copyfile(data, stored)
        else:
            stored.write_bytes(data)
        files.append(
            {
                "upload_id": upload_id,
                "name": safe,
                "stored": str(stored),
                "sha256": digest,
                "size": size,
                "doc_id": doc_id_for_name(safe),
                "status": "uploaded",
                "modes": {},
                "error": "",
            }
        )
        return job

    def remove_uploads(self, job_id: str) -> None:
        shutil.rmtree(self._dir(job_id) / "uploads", ignore_errors=True)
