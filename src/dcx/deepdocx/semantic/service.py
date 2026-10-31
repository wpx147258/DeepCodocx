"""Persistent host jobs for semantic indexes, independent of Agent Runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...atomic import atomic_write_text
from ..ingest.control import IngestCancelled, locked
from ..library import DocumentLibrary
from .client import InferenceClient
from .engine import SemanticEngine
from .settings import CaptionConfig, IndexConfig

TERMINAL = {"succeeded", "partial", "failed", "cancelled"}


class IndexService:
    def __init__(
        self,
        data_dir: Path,
        index: IndexConfig,
        caption: CaptionConfig,
        env: Mapping[str, str] | None = None,
    ):
        self.data_dir, self.index, self.caption = data_dir, index, caption
        self.env = os.environ if env is None else env
        self.root = data_dir / "semantic"
        self._processes: dict[str, subprocess.Popen[bytes]] = {}

    def engine(self, library: DocumentLibrary) -> SemanticEngine:
        return SemanticEngine(
            library,
            self.data_dir,
            self.index,
            self.caption,
            InferenceClient(self.index, self.caption, self.env),
        )

    def status(self, library: DocumentLibrary) -> dict[str, Any]:
        engine = self.engine(library)
        payload = engine.status()
        jobs = engine.store.jobs()
        for job in jobs:
            if job["status"] in TERMINAL:
                process = self._processes.get(job["id"])
                if process is not None and process.poll() is not None:
                    self._processes.pop(job["id"], None)
                continue
            if (
                job["status"] == "queued"
                and job.get("pid") is None
                and time.time() - job["created"] < 10
            ):
                continue
            process = self._processes.get(job["id"])
            alive = process.poll() is None if process is not None else self._alive(job)
            if not alive:
                job = engine.store.update_job(
                    job["id"],
                    status="failed",
                    error="The index worker stopped unexpectedly. Synchronize again to resume cached progress.",
                )
            if job["status"] in TERMINAL:
                self._processes.pop(job["id"], None)
        return {
            **payload,
            "enabled": self.index.enabled,
            "embedding_ready": engine.client.ready(),
            "caption_ready": engine.client.ready(True),
            "caption_model": engine.caption.model,
            "api_key_env": self.index.api_key_env,
            "caption_key_env": self.caption.api_key_env,
            "jobs": engine.store.jobs(),
        }

    def _alive(self, job: dict[str, Any]) -> bool:
        pid = job.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        cmdline = Path(f"/proc/{pid}/cmdline")
        try:
            args = cmdline.read_bytes().split(b"\0")
            return b"dcx.deepdocx.semantic.worker" in args and os.fsencode(job["spec_path"]) in args
        except OSError:
            return False

    def start(
        self,
        library: DocumentLibrary,
        *,
        generate: bool | None = None,
        force: bool = False,
        automatic: bool = False,
        caption_model: str | None = None,
        documents: list[str] | None = None,
    ) -> dict[str, Any]:
        if documents is None and library.scope:
            documents = list(library.scope)
        if documents is not None:
            if (
                not isinstance(documents, list)
                or not documents
                or len(documents) > 10000
                or not all(isinstance(doc, str) and doc for doc in documents)
            ):
                raise ValueError("documents must be a nonempty array of document IDs")
            documents = sorted(set(documents))
            known = {item["doc_id"] for item in library.documents()}
            if set(documents) - known:
                raise ValueError(
                    "Unknown or out-of-scope documents: "
                    + ", ".join(sorted(set(documents) - known))
                )
        if caption_model is not None and (
            not isinstance(caption_model, str)
            or caption_model
            not in {
                "qwen3.8-flash",
                "DeepSeek-V4.1-Flash-1017",
            }
        ):
            raise ValueError("Unsupported caption model")
        engine = self.engine(library)
        if not self.index.enabled:
            raise ValueError("Semantic indexing is disabled in configuration")
        if not engine.client.ready():
            raise ValueError(f"Set {self.index.api_key_env} in the server environment")
        if generate is not None and generate and not engine.client.ready(True):
            raise ValueError(f"Set {self.caption.api_key_env} for generated captions")
        with locked(self.root / library.id / "start.lock"):
            current = self.status(library)
            active = next((job for job in current["jobs"] if job["status"] not in TERMINAL), None)
            if active:
                if automatic:
                    pending = engine.store.preference("resync_documents")
                    if documents is None or (
                        engine.store.preference("resync_requested") and not pending
                    ):
                        pending = []  # Full-library pass dominates a scoped pass.
                    else:
                        pending = sorted(set(pending or []) | set(documents))
                    engine.store.preference("resync_documents", pending)
                    engine.store.preference("resync_requested", True)
                elif active.get("documents") != documents:
                    raise ValueError(
                        "An index job with a different document selection is running; stop it before starting another selection"
                    )
                return active
            if caption_model is not None:
                engine.store.preference("caption_model", caption_model)
                engine = self.engine(library)
            if generate is not None:
                engine.store.preference("generate_captions", generate)
            if not automatic:
                engine.store.preference("auto_sync", True)
            engine.store.preference("resync_requested", False)
            engine.store.preference("resync_documents", [])
            ident = uuid.uuid4().hex[:12]
            directory = self.root / library.id / "jobs" / ident
            directory.mkdir(parents=True)
            spec_path = directory / "settings.json"
            spec = {
                "id": ident,
                "library_root": str(library.root),
                "data_dir": str(self.data_dir),
                "index": asdict(self.index),
                "caption": asdict(self.caption),
                "documents": documents,
            }
            atomic_write_text(spec_path, json.dumps(spec))
            job = engine.store.update_job(
                ident,
                library_id=library.id,
                force=force,
                generate=engine.generate,
                pid=None,
                spec_path=str(spec_path),
                documents=documents,
                total=len(documents) if documents is not None else len(library.documents()),
            )
            process_env = dict(os.environ)
            for key in (self.index.api_key_env, self.caption.api_key_env, self.index.joint_key_env):
                process_env.pop(key, None)
                if self.env.get(key):
                    process_env[key] = self.env[key]
            try:
                # Worker handshake waits until the parent publishes its PID.
                with (directory / "worker.log").open("ab") as log:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "dcx.deepdocx.semantic.worker",
                            "--settings",
                            str(spec_path),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        env=process_env,
                        start_new_session=True,
                    )
                self._processes[ident] = process
                job = engine.store.update_job(ident, pid=process.pid)
            except OSError as exc:
                engine.store.update_job(ident, status="failed", error=str(exc))
                raise
            return job

    def automatic(
        self, library: DocumentLibrary, documents: list[str] | None = None
    ) -> dict[str, Any] | None:
        engine = self.engine(library)
        auto = engine.store.preference("auto_sync")
        if (
            not self.index.enabled
            or not (self.index.auto_sync if auto is None else auto)
            or not engine.client.ready()
        ):
            return None
        return self.start(library, automatic=True, documents=documents)

    def cancel(self, library: DocumentLibrary, ident: str) -> dict[str, Any]:
        store = self.engine(library).store
        job = store.job(ident)
        if job["status"] in TERMINAL:
            return job
        store.preference("resync_requested", False)
        store.preference("resync_documents", [])
        return store.update_job(ident, status="cancelled", error="Stopped by user")


def run_worker(spec_path: Path) -> int:
    raw = json.loads(spec_path.read_text())
    from .settings import parse_settings

    index, caption = parse_settings(raw["index"], raw["caption"])
    library = DocumentLibrary(Path(raw["library_root"]))
    service = IndexService(Path(raw["data_dir"]), index, caption)
    engine = service.engine(library)
    store = engine.store
    ident = raw["id"]

    def check():
        if store.job(ident)["status"] == "cancelled":
            raise IngestCancelled("Stopped by user")

    engine.client.check = check
    try:
        # The parent owns start.lock until PID persistence; workers then serialize
        # across libraries to bound inference concurrency and allow queued cancel.
        with locked(service.root / library.id / "start.lock", check):
            pass
        with locked(service.root / "worker.lock", check, timeout=None):
            check()
            job = store.job(ident)
            store.update_job(ident, status="running")

            def progress(done, total, title):
                check()
                store.update_job(ident, processed=done, total=total, current=title)

            result = engine.sync(
                force=job.get("force", False),
                documents=raw.get("documents"),
                check=check,
                progress=progress,
            )
            # An ingestion publication during the job requests one more incremental
            # pass, without spawning a second competing writer.
            while True:
                check()
                with locked(service.root / library.id / "start.lock", check):
                    if not store.preference("resync_requested"):
                        status = (
                            "partial"
                            if result["errors"] and result["completed_documents"]
                            else "failed"
                            if result["errors"]
                            else "succeeded"
                        )
                        store.update_job(
                            ident,
                            status=status,
                            result=result,
                            errors=result["errors"],
                            current=None,
                            processed=result["completed_documents"] + result["unchanged_documents"],
                            total=result["selected_documents"],
                        )
                        break
                    follow_documents = store.preference("resync_documents") or None
                    store.preference("resync_requested", False)
                    store.preference("resync_documents", [])
                follow = engine.sync(documents=follow_documents, check=check, progress=progress)
                result["completed_documents"] += follow["completed_documents"]
                result["unchanged_documents"] += follow["unchanged_documents"]
                result["selected_documents"] += follow["selected_documents"]
                result["errors"].extend(follow["errors"])
    except IngestCancelled:
        pass
    except Exception as exc:
        store.update_job(ident, status="failed", error=str(exc))
    return 0 if store.job(ident)["status"] == "succeeded" else 1
