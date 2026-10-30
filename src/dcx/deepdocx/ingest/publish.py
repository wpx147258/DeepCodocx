"""Validate and publish a complete rendition, rolling back failed replacements."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from ...atomic import atomic_write_text
from .control import locked
from .identity import sha256_file

REQUIRED = {
    "basic": ("document.json", "chunks.jsonl", "pages.json", "assets/index.json"),
    "marker": ("source.json", "content.json", "units.jsonl", "pages.json", "assets/index.json"),
}
SCHEMA = {"basic": "dda.bundle/1", "marker": "dda.bundle/3"}


class PublishError(ValueError):
    pass


def notify_library(library_root: Path) -> None:
    # Read by both Web and persistent stdio MCP consumers, without a runtime hook.
    atomic_write_text(library_root / ".dcx-revision", uuid.uuid4().hex)


def validate_bundle(path: Path, rendition: str) -> dict[str, Any]:
    if rendition not in REQUIRED:
        raise PublishError("Unknown rendition")
    if any(item.is_symlink() for item in path.rglob("*")):
        raise PublishError("Bundle files must not be symlinks")
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
    try:
        pages = json.loads((path / "pages.json").read_text())["pages"]
        assets = json.loads((path / "assets/index.json").read_text())["assets"]
        if not isinstance(pages, list) or not pages or not isinstance(assets, dict):
            raise ValueError("Missing page previews")
        for page in pages:
            asset = assets[page["render_asset"]]
            image = (path / asset["path"]).resolve()
            if not image.is_relative_to(path.resolve()) or not image.is_file():
                raise ValueError("Missing page image")
        for relative, info in raw.get("files", {}).items():
            file = (path / relative).resolve()
            if not file.is_relative_to(path.resolve()) or sha256_file(file) != info["sha256"]:
                raise ValueError(f"Invalid file checksum: {relative}")
        checksum = path / "manifest.sha256"
        if checksum.is_file() and checksum.read_text().strip() != sha256_file(manifest):
            raise ValueError("Invalid manifest checksum")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise PublishError(f"Invalid bundle contents: {exc}") from exc
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
    if rendition not in REQUIRED or not re.fullmatch(r"[A-Za-z0-9_-]+", doc_id):
        raise PublishError("Invalid rendition or document id")
    built = staging / doc_id / "v1" / rendition
    raw = validate_bundle(built, rendition)
    doc = raw.get("doc", {})
    if not isinstance(doc, dict):
        raise PublishError("Invalid bundle identity")
    version = str(doc.get("version") or raw.get("source_version") or "v1")
    if version != "v1" or (doc.get("doc_id") or raw.get("doc_id")) != doc_id:
        raise PublishError("Bundle identity does not match the upload")
    target = library_root / doc_id / version / rendition
    with locked(library_root / ".dcx-publish.lock"):
        if target.exists() and not force:
            validate_bundle(target, rendition)
            return {"status": "skipped", "target": str(target), "warnings": []}
        if not target.resolve().is_relative_to(library_root.resolve()) or target.is_symlink():
            raise PublishError("Publish target is outside the document library")
        bundled_source = built / "source" / "original.pdf"
        bundled_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_pdf, bundled_source)
        source_dir = library_root.parent / "sources"
        source_dir.mkdir(parents=True, exist_ok=True)
        source_target = source_dir / f"{doc_id}.pdf"
        if source_target.is_symlink():
            raise PublishError("Source destination must not be a symlink")
        incoming_hash = sha256_file(source_pdf)
        known_hashes = {sha256_file(source_target)} if source_target.is_file() else set()
        for metadata in (library_root / doc_id / version).glob("*/source.json"):
            source_info = json.loads(metadata.read_text(encoding="utf-8"))
            if isinstance(source_info, dict) and source_info.get("source_sha256"):
                known_hashes.add(source_info["source_sha256"])
        if any(digest != incoming_hash for digest in known_hashes):
            raise PublishError(
                "A different PDF already uses this filename. Rename the new PDF to keep document renditions consistent; replace only rebuilds the same source."
            )
        scratch = library_root.parent / ".dcx-recovery" / uuid.uuid4().hex
        scratch.mkdir(parents=True)
        backup = scratch / "old-bundle"
        old_source = scratch / "old-source.pdf"
        new_source = scratch / "new-source.pdf"
        source_existed = source_target.exists()
        committed = False
        source_changed = False
        preserve_recovery = False
        try:
            shutil.copy2(source_pdf, new_source)
            if source_existed:
                shutil.copy2(source_target, old_source)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.rename(backup)
            try:
                built.rename(target)
                committed = True
                os.replace(new_source, source_target)
                source_changed = True
                notify_library(library_root)
            except BaseException:
                try:
                    if committed:
                        shutil.rmtree(target)
                    if backup.exists():
                        backup.rename(target)
                    if source_changed:
                        if source_existed:
                            os.replace(old_source, source_target)
                        else:
                            source_target.unlink(missing_ok=True)
                except BaseException as exc:
                    preserve_recovery = True
                    raise PublishError(
                        f"Publish rollback needs manual recovery; original files kept at {scratch}: {exc}"
                    ) from exc
                raise
        finally:
            if not preserve_recovery:
                shutil.rmtree(scratch, ignore_errors=True)
        for parent in (built.parent, built.parent.parent):
            with contextlib.suppress(OSError):
                parent.rmdir()
        return {"status": "published", "target": str(target), "warnings": []}
