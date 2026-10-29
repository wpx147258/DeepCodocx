"""Background worker: build basic and/or Marker bundles, then publish/index."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

from .builders import BuildError, build_basic, build_marker
from .index import build_basic_index
from .jobs import JobStore
from .publish import PublishError, publish


def _set_file(job: dict[str, Any], index: int, **changes: Any) -> None:
    job["files"][index].update(changes)


def _build_one(
    job: dict[str, Any],
    index: int,
    *,
    rendition: str,
    staging: Path,
) -> dict[str, Any]:
    entry = job["files"][index]
    pdf = Path(entry["stored"])
    doc_id = entry["doc_id"]
    if rendition == "basic":
        result = build_basic(
            pdf, staging, doc_id=doc_id, dpi=int(job["settings"]["basic_dpi"])
        )
        if result.get("doc_id") != doc_id:
            raise BuildError("Builder used an unexpected document id")
    else:
        result = build_marker(
            pdf,
            staging,
            doc_id,
            marker_command=str(job["settings"]["marker_command"]),
            timeout=int(job["settings"]["marker_timeout"]),
        )
    return publish(
        staging,
        Path(job["library_root"]),
        doc_id,
        rendition,
        pdf,
        force=bool(job.get("force")),
    )


def run(job_path: Path) -> int:
    store = JobStore(job_path.parent.parent)
    job_id = job_path.parent.name
    job = store.get(job_id)
    job["status"] = "running"
    store.write(job)
    staging = Path(job["library_root"]).parent / ".dcx-ingest" / job_id
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    mode = str(job["mode"])
    files = job["files"]
    warnings: list[str] = list(job.get("warnings") or [])
    try:
        for phase in ("basic", "marker"):
            if phase == "basic" and mode not in {"basic", "both"}:
                continue
            if phase == "marker" and mode not in {"marker", "both"}:
                continue
            for index, entry in enumerate(files):
                job = store.get(job_id)
                if job.get("status") == "cancelled":
                    return 0
                _set_file(job, index, status="running", current_mode=phase)
                job["current"] = {"index": index, "name": entry["name"], "mode": phase}
                store.write(job)
                try:
                    result = _build_one(job, index, rendition=phase, staging=staging)
                    _set_file(
                        job,
                        index,
                        status="ok" if result["status"] == "published" else "skipped",
                        **{f"{phase}_status": result["status"]},
                    )
                    entry = job["files"][index]
                    modes = dict(entry.get("modes") or {})
                    modes[phase] = result["status"]
                    _set_file(job, index, modes=modes, error="")
                    warnings.extend(result.get("warnings") or [])
                except (BuildError, PublishError, OSError, ValueError, KeyError) as exc:
                    _set_file(
                        job,
                        index,
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                        **{f"{phase}_status": "failed"},
                    )
                    modes = dict(job["files"][index].get("modes") or {})
                    modes[phase] = "failed"
                    _set_file(job, index, modes=modes)
                    warnings.append(f"{entry['name']} {phase}: {type(exc).__name__}: {exc}")
                store.write(job)

            if phase == "basic" and mode in {"basic", "both"}:
                job = store.get(job_id)
                job["status"] = "indexing"
                store.write(job)
                try:
                    stats = build_basic_index(Path(job["library_root"]))
                    job.setdefault("result", {})["index"] = stats
                except (OSError, ValueError) as exc:
                    warnings.append(f"Index rebuild failed: {type(exc).__name__}: {exc}")
                if mode == "both":
                    job = store.get(job_id)
                    job["status"] = "running"
                    store.write(job)

        job = store.get(job_id)
        statuses = [str(item.get("status")) for item in files]
        errors = sum(1 for status in statuses if status == "failed")
        if errors == 0:
            job["status"] = "succeeded"
        elif errors == len(files):
            job["status"] = "failed"
        else:
            job["status"] = "partial"
        job["warnings"] = warnings
        job["current"] = None
        store.write(job)
        if job["status"] in {"succeeded", "partial"}:
            store.remove_uploads(job_id)
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args(argv)
    return run(args.job.resolve())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
