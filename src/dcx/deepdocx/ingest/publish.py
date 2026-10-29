"""Validate and atomically publish a built rendition into a document library."""

from __future__ import annotations

import contextlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

REQUIRED = {
    "basic": ("document.json", "chunks.jsonl", "pages.json", "assets/index.json"),
    "marker": ("content.json", "units.jsonl", "pages.json", "assets/index.json"),
}
SCHEMA = {"basic": "dda.bundle/1", "marker": "dda.bundle/3"}


class PublishError(ValueError):
    pass


def validate_bundle(path: Path, rendition: str) -> dict[str, Any]:
    if rendition not in REQUIRED:
        raise PublishError("Unknown rendition")
    manifest = path / "manifest.json"
    if not manifest.is_file():
        raise PublishError(f"Missing manifest: {path.name}")
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"Invalid manifest: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA[rendition]:
        raise PublishError(f"Unexpected schema for {rendition} bundle")
    for name in REQUIRED[rendition]:
        if not (path / name).is_file():
            raise PublishError(f"Missing bundle file: {name}")
    return raw


def publish(
    staging: Path,
    library_root: Path,
    doc_id: str,
    rendition: str,
    source_pdf: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Publish one rendition. Returns published/skipped plus warnings."""
    if rendition not in REQUIRED:
        raise PublishError("Unknown rendition")
    built = staging / doc_id / "v1" / rendition
    raw = validate_bundle(built, rendition)
    version = str(raw.get("doc", {}).get("version") or raw.get("source_version") or "v1")
    target = library_root / doc_id / version / rendition
    warnings: list[str] = []

    source_dir = library_root.parent / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_target = source_dir / f"{doc_id}.pdf"
    if source_target.is_file():
        # Keep the first source; do not silently replace a different source.
        if source_target.stat().st_size != source_pdf.stat().st_size:
            warnings.append("A different source PDF already exists for this doc_id")
    else:
        shutil.copy2(source_pdf, source_target)

    # The bundle should remain self-contained for future high-resolution reads.
    bundled_source = built / "source" / "original.pdf"
    if not bundled_source.is_file():
        bundled_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_pdf, bundled_source)
        # Basic manifests do not know about source/original.pdf; it is an extra file.

    if target.exists():
        if not force:
            shutil.rmtree(built, ignore_errors=True)
            return {"status": "skipped", "target": str(target), "warnings": warnings}
        backup = staging / f".backup-{uuid.uuid4().hex[:8]}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(backup))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(built), str(target))
    # Remove now-empty version/doc dirs without touching unrelated renditions.
    for parent in (built.parent, built.parent.parent):
        with contextlib.suppress(OSError):
            parent.rmdir()
    return {"status": "published", "target": str(target), "warnings": warnings}
