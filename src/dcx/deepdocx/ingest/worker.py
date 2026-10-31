"""Background writer: fixed converters, publish and index; no model or runtime."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

from .builders import build_basic, build_marker
from .control import IngestCancelled, locked
from .index import update_basic_index
from .jobs import TERMINAL, JobStore
from .publish import publish, validate_bundle


def run(job_path: Path) -> int:
    store = JobStore(job_path.parent.parent)
    job_id = job_path.parent.name
    initial = store.get(job_id)
    staging = Path(initial["library_root"]).parent / ".dcx-ingest" / job_id
    dirty_basic: set[str] = set()

    def cancelled() -> bool:
        return store.get(job_id)["status"] == "cancelled"

    def check() -> None:
        if cancelled():
            raise IngestCancelled("Cancelled by user")

    def index_published() -> None:
        if not dirty_basic:
            return
        with store.edit(job_id) as job:
            if job["status"] not in TERMINAL:
                job.update(status="indexing", current=None)
        stats = update_basic_index(Path(initial["library_root"]), sorted(dirty_basic))
        with store.edit(job_id) as job:
            job.setdefault("result", {})["index"] = stats
        dirty_basic.clear()

    try:
        # Serialize host workers across CLI, browsers and service restarts. This
        # also bounds Marker/GPU use; the agent can keep running independently.
        with locked(store.root.parent / "worker.lock", cancelled, timeout=None):
            check()
            staging.mkdir(parents=True, exist_ok=True)
            with store.edit(job_id) as job:
                check()
                job["status"] = "running"
            phases = ("basic", "marker") if initial["mode"] == "both" else (initial["mode"],)
            for phase in phases:
                for index in range(len(initial["files"])):
                    check()
                    with store.edit(job_id) as job:
                        check()
                        entry = job["files"][index]
                        entry.update(status="running", current_mode=phase)
                        job.update(
                            status="running",
                            current={"index": index, "name": entry["name"], "mode": phase},
                        )
                    try:
                        pdf = Path(entry["stored"])
                        doc_id = entry["doc_id"]
                        target = Path(job["library_root"]) / doc_id / "v1" / phase
                        if target.is_dir() and not job.get("force"):
                            validate_bundle(target, phase)
                            result: dict[str, Any] = {"status": "skipped", "warnings": []}
                        else:
                            if phase == "basic":
                                built = build_basic(
                                    pdf,
                                    staging,
                                    doc_id=doc_id,
                                    dpi=int(job["settings"]["basic_dpi"]),
                                    cancelled=cancelled,
                                )
                                if built.get("doc_id") != doc_id:
                                    raise ValueError("Builder used an unexpected document id")
                            else:
                                build_marker(
                                    pdf,
                                    staging,
                                    doc_id,
                                    marker_command=str(job["settings"]["marker_command"]),
                                    timeout=int(job["settings"]["marker_timeout"]),
                                    cancelled=cancelled,
                                )
                            check()
                            # Cancel waits until a complete rendition is committed;
                            # it cannot be overwritten by a stale worker snapshot.
                            with store.edit(job_id) as latest:
                                check()
                                result = publish(
                                    staging,
                                    Path(job["library_root"]),
                                    doc_id,
                                    phase,
                                    pdf,
                                    force=bool(job.get("force")),
                                )
                                latest["files"][index].setdefault("modes", {})[phase] = result[
                                    "status"
                                ]
                        if phase == "basic":
                            dirty_basic.add(doc_id)
                        with store.edit(job_id) as job:
                            check()
                            item = job["files"][index]
                            item.setdefault("modes", {})[phase] = result["status"]
                            item.update(
                                status="ok" if result["status"] == "published" else "skipped",
                                error="",
                            )
                            job["warnings"].extend(result.get("warnings", []))
                    except IngestCancelled:
                        raise
                    except (
                        Exception
                    ) as exc:  # A malformed PDF fails this rendition, not the worker.
                        with store.edit(job_id) as job:
                            check()
                            item = job["files"][index]
                            item.setdefault("modes", {})[phase] = "failed"
                            item.update(
                                status="failed", error=f"{phase}: {type(exc).__name__}: {exc}"
                            )
                            job["warnings"].append(f"{item['name']}: {item['error']}")
                if phase == "basic":
                    index_published()
            with store.edit(job_id) as job:
                check()
                results = [value for item in job["files"] for value in item["modes"].values()]
                failed = any(value == "failed" for value in results)
                usable = any(value in {"published", "skipped"} for value in results)
                job.update(
                    status="partial" if failed and usable else "failed" if failed else "succeeded",
                    current=None,
                )
                for item in job["files"]:
                    values = list(item["modes"].values())
                    item["status"] = (
                        "partial"
                        if "failed" in values and any(v != "failed" for v in values)
                        else "failed"
                        if "failed" in values
                        else "skipped"
                        if all(v == "skipped" for v in values)
                        else "ok"
                    )
                    item.pop("current_mode", None)
    except IngestCancelled:
        # Already committed Basic renditions must remain searchable after cancel.
        try:
            index_published()
        except Exception as exc:
            with store.edit(job_id) as job:
                job["warnings"].append(f"Index update failed: {exc}")
    except Exception as exc:
        with store.edit(job_id) as job:
            if job["status"] != "cancelled":
                usable = any(
                    value in {"published", "skipped"}
                    for item in job["files"]
                    for value in item["modes"].values()
                )
                job.update(
                    status="partial" if usable else "failed",
                    current=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if store.get(job_id)["status"] in {"succeeded", "partial", "cancelled"}:
            store.remove_uploads(job_id)
    # Derived indexing is a separate task. Its failure never changes conversion
    # success or writes anything into the agent's transcript.
    final = store.get(job_id)
    if final.get("semantic") and any(
        value == "published" for item in final["files"] for value in item["modes"].values()
    ):
        try:
            from ..library import DocumentLibrary
            from ..semantic.service import IndexService
            from ..semantic.settings import parse_settings

            settings = final["semantic"]
            index_settings, caption_settings = parse_settings(
                settings["index"], settings["caption"]
            )
            published_docs = [
                item["doc_id"]
                for item in final["files"]
                if any(value == "published" for value in item["modes"].values())
            ]
            task = IndexService(
                Path(settings["data_dir"]), index_settings, caption_settings
            ).automatic(DocumentLibrary(Path(final["library_root"])), documents=published_docs)
            with store.edit(job_id) as updated:
                updated.setdefault("result", {})["semantic_job_id"] = task["id"] if task else None
        except Exception as exc:
            with store.edit(job_id) as updated:
                updated["warnings"].append(f"Semantic indexing could not be queued: {exc}")
    return 0 if store.get(job_id)["status"] == "succeeded" else 1


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args(argv)
    return run(args.job.resolve())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
