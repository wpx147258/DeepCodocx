"""Write a PDF Bundle v3 from Marker JSON."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .parser import ParsedPdf

BUNDLE_SCHEMA = "dda.bundle/3"
PAGE_MODEL = "pdf"
RENDITION = "marker-json-v1"


class BundleError(ValueError):
    pass


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _unit(block, index: int, doc_id: str, version: str) -> dict[str, Any]:
    text = {"body": block.text, "caption": block.caption, "ocr": None, "description": None}
    if block.kind == "figure":
        text["caption"] = block.caption or block.text
        text["body"] = None
    payload = {
        "unit_id": f"u_{index:06d}",
        "unit_type": block.kind,
        "modality": (
            ["text", "image"] if block.kind in {"figure", "table"} and block.asset_ids else ["text"]
        ),
        "doc_id": doc_id,
        "source_version": version,
        "rendition_id": RENDITION,
        "page_model": PAGE_MODEL,
        "page": block.page,
        "bbox": list(block.bbox),
        "page_kind": "physical",
        "section_path": list(block.section_path),
        "labels": [block.label] if block.label else [],
        "text": text,
        "assets": {"images": list(block.asset_ids)},
        "links": {"parent_section": None, "prev": None, "next": None, "mentioned_by": []},
        "provenance": {"text_source": "marker", "layout_source": "marker"},
    }
    if block.level:
        payload["heading_level"] = block.level
    if block.rows:
        payload["rows"] = [list(row) for row in block.rows]
        payload["html"] = block.html
    if block.items:
        payload["items"] = list(block.items)
    return payload


def write_pdf_bundle(
    parsed: ParsedPdf,
    out_root: Path,
    *,
    doc_id: str | None = None,
    title: str | None = None,
    license: str | None = None,
    source_version: str | None = None,
) -> Path:
    source_sha = parsed.source_sha256
    version = source_version or f"v_{source_sha[:16]}"
    if re.fullmatch(r"[A-Za-z0-9_-]+", version) is None:
        raise BundleError("invalid source version")
    doc_id = doc_id or f"pdf_{source_sha[:12]}"
    if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", doc_id) is None:
        raise BundleError(f"invalid doc_id: {doc_id}")
    final = out_root.resolve() / doc_id / version / RENDITION
    if final.exists():
        raise BundleError(f"bundle already exists: {final}")
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = final.parent / f".{RENDITION}.staging-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    staging.mkdir()
    try:
        (staging / "source").mkdir()
        shutil.copyfile(parsed.path, staging / "source" / "original.pdf")
        (staging / "assets" / "files").mkdir(parents=True)
        (staging / "review").mkdir()
        asset_index = {}
        asset_map: dict[str, str] = {}
        for asset_id, source in parsed.assets.items():
            if source is None or not source.is_file():
                continue
            suffix = source.suffix.lower() or ".bin"
            relative = f"assets/files/{asset_id}{suffix}"
            target = staging / relative
            shutil.copyfile(source, target)
            asset_index[asset_id] = {
                "path": relative,
                "mime": "image/png",
                "sha256": _sha(target),
            }
            asset_map[asset_id] = relative
        page_assets = {}
        for page_number, source in parsed.page_images.items():
            if not source.is_file():
                continue
            asset_id = f"as_page_{page_number:04d}"
            relative = f"assets/files/{asset_id}.png"
            shutil.copyfile(source, staging / relative)
            asset_index[asset_id] = {
                "path": relative,
                "mime": "image/png",
                "sha256": _sha(staging / relative),
            }
            page_assets[page_number] = asset_id
        _json(staging / "assets" / "index.json", {"schema": "dda.assets/3", "assets": asset_index})
        _json(
            staging / "source.json",
            {
                "schema": "dda.source/3",
                "format": "pdf",
                "page_model": PAGE_MODEL,
                "source_sha256": source_sha,
                "original_name": parsed.path.name,
                "original_path_at_ingest": str(parsed.path),
                "backup": "source/original.pdf",
                "backup_optional": True,
                "mime": "application/pdf",
                "license": license,
                "title": title,
            },
        )
        _json(
            staging / "pages.json",
            {
                "schema": "dda.pages/3",
                "page_model": PAGE_MODEL,
                "pages": [
                    {
                        "page": page.page,
                        "kind": "physical",
                        "width": page.width,
                        "height": page.height,
                        "rotation": 0,
                        "render_asset": page_assets.get(page.page),
                    }
                    for page in parsed.pages
                ],
            },
        )
        blocks = [
            {
                "block_id": block.block_id,
                "type": block.kind,
                "page": block.page,
                "bbox": list(block.bbox),
                "text": block.text,
                "html": block.html,
                "section_path": list(block.section_path),
                "level": block.level,
                "label": block.label,
                "caption": block.caption,
                "rows": [list(row) for row in block.rows],
                "items": list(block.items),
                "asset_ids": list(block.asset_ids),
            }
            for block in parsed.blocks
        ]
        _json(
            staging / "content.json",
            {"schema": "dda.content/3", "page_model": PAGE_MODEL, "blocks": blocks},
        )
        units: list[dict[str, Any]] = []
        index = 0
        for page in parsed.pages:
            if page.page not in page_assets:
                continue
            index += 1
            units.append(
                {
                    "unit_id": f"u_{index:06d}",
                    "unit_type": "page",
                    "modality": ["image"] if page.page in page_assets else [],
                    "doc_id": doc_id,
                    "source_version": version,
                    "rendition_id": RENDITION,
                    "page_model": PAGE_MODEL,
                    "page": page.page,
                    "bbox": [0.0, 0.0, page.width, page.height],
                    "page_kind": "physical",
                    "section_path": [],
                    "labels": [],
                    "text": {"body": "", "caption": None, "ocr": None, "description": None},
                    "assets": {
                        "images": [page_assets[page.page]] if page.page in page_assets else []
                    },
                    "links": {
                        "parent_section": None,
                        "prev": None,
                        "next": None,
                        "mentioned_by": [],
                    },
                    "provenance": {"text_source": "none", "layout_source": "marker"},
                }
            )
        for block in parsed.blocks:
            index += 1
            units.append(_unit(block, index, doc_id, version))
        for left, right in zip(units, units[1:], strict=False):
            left["links"]["next"] = right["unit_id"]
            right["links"]["prev"] = left["unit_id"]
        (staging / "units.jsonl").write_text(
            "".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units),
            encoding="utf-8",
        )
        if parsed.presentation is not None:
            (staging / "presentation.md").write_text(parsed.presentation, encoding="utf-8")
        _json(
            staging / "quality.json",
            {
                "schema": "dda.quality/3",
                "page_model": PAGE_MODEL,
                "derived_status": "missing",
                "human_reviewed": False,
                "blocks": len(parsed.blocks),
                "units": len(units),
                "assets": len(asset_index),
                "warnings": list(parsed.warnings),
            },
        )
        _json(
            staging / "review" / "status.json",
            {
                "schema": "dda.review/1",
                "status": "machine_parsed",
                "human_reviewed": False,
                "scope": "automatic",
            },
        )
        _json(
            staging / "diagnostics.json",
            {"schema": "dda.diagnostics/3", "warnings": list(parsed.warnings)},
        )
        files = {}
        for path in sorted(staging.rglob("*")):
            if path.is_file() and path.name not in {"manifest.json", "manifest.sha256"}:
                files[str(path.relative_to(staging))] = {
                    "sha256": _sha(path),
                    "bytes": path.stat().st_size,
                }
        manifest = {
            "schema": BUNDLE_SCHEMA,
            "doc_id": doc_id,
            "source_version": version,
            "rendition_id": RENDITION,
            "page_model": PAGE_MODEL,
            "format": "pdf",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "parser": {"name": "marker-json", "version": "1.10.2"},
            "files": files,
        }
        _json(staging / "manifest.json", manifest)
        (staging / "manifest.sha256").write_text(
            hashlib.sha256((staging / "manifest.json").read_bytes()).hexdigest() + "\n",
            encoding="utf-8",
        )
        os.replace(staging, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final
