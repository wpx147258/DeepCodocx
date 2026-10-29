"""Persistent JSON job records for document ingestion.

Jobs are deliberately outside the runtime and outside the library. They hold
uploaded source paths, mode, progress and errors; the worker mutates one job.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...atomic import atomic_write_text
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
        if not job_id or "/" in job_id or "\\" in job_id:
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
                jobs.append(json.loads(path.read_text(encoding="utf-8")))
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
        job = self.get(job_id)
        job.update(changes)
        self.write(job)
        return job

    def add_file(self, job_id: str, name: str, data: bytes, *, max_bytes: int) -> dict[str, Any]:
        if len(data) > max_bytes:
            raise ValueError(f"File exceeds the {max_bytes // (1024 * 1024)} MiB limit")
        if not data.startswith(PDF_MAGIC):
            raise ValueError("Only PDF uploads are supported in this version")
        safe = sanitize_filename(name)
        if not safe.lower().endswith(".pdf"):
            safe += ".pdf"
        job = self.get(job_id)
        if job.get("status") not in {"uploading", "queued"}:
            raise ValueError("This job is no longer accepting uploads")
        files = job.setdefault("files", [])
        digest = sha256_bytes(data)
        if any(item.get("sha256") == digest for item in files):
            return job
        upload_id = f"{len(files) + 1:03d}"
        upload_dir = self._dir(job_id) / "uploads" / upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored = upload_dir / safe
        stored.write_bytes(data)
        files.append(
            {
                "upload_id": upload_id,
                "name": safe,
                "stored": str(stored),
                "sha256": digest,
                "size": len(data),
                "doc_id": doc_id_for_name(safe),
                "status": "uploaded",
                "modes": {},
                "error": "",
            }
        )
        self.write(job)
        return job

    def remove_uploads(self, job_id: str) -> None:
        shutil.rmtree(self._dir(job_id) / "uploads", ignore_errors=True)
