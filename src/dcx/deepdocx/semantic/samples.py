"""Normalize heterogeneous bundles into bounded semantic blocks and image samples."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..library import DocumentLibrary, Rendition

PIPELINE = "blocks-v1"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def file_hash(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


@dataclass
class Sample:
    key: str
    family: str
    doc_id: str
    version: str
    rendition: str
    locator: str
    title: str
    page: int
    text: str = ""
    image: str = ""
    asset_id: str = ""
    caption: str = ""
    context: str = ""
    sources: list[str] = field(default_factory=list)
    bbox: list[float] | None = None
    image_kind: str = ""
    context_sources: list[str] = field(default_factory=list)
    heading_path: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def fingerprint(
    library: DocumentLibrary, entries: list[Rendition], hasher: Callable[[Path], str] = file_hash
) -> str:
    """Hash only consumed sources and image bytes; bundle integrity stays untouched."""
    files: list[tuple[str, ...]] = []
    for entry in sorted(entries, key=lambda e: (e.doc_id, e.version, e.rendition)):
        names = (
            "manifest.json",
            "source.json",
            "document.json",
            "chunks.jsonl",
            "content.json",
            "units.jsonl",
            "pages.json",
            "assets/index.json",
        )
        for name in names:
            path = entry.path / name
            if path.is_file():
                path = library.safe_path(entry, name)
                files.append((str(path.relative_to(library.root)), hasher(path)))
        assets = library.metadata(entry.path / "assets/index.json").get("assets", {})
        for key, asset in sorted(assets.items()):
            if str(asset.get("mime", "")).startswith("image/"):
                path = library.safe_path(entry, asset["path"])
                files.append((key, str(path.relative_to(library.root)), hasher(path)))
    return digest({"pipeline": PIPELINE, "files": files})


def documents(library: DocumentLibrary) -> dict[str, list[Rendition]]:
    result: dict[str, list[Rendition]] = {}
    for entry in library.entries():
        result.setdefault(entry.doc_id, []).append(entry)
    return result


def _blocks(units: list[dict[str, Any]], basic: bool, limit: int) -> list[list[dict[str, Any]]]:
    texts = [
        u
        for u in units
        if u.get("text", "").strip() and u.get("kind") not in {"page", "figure", "table"}
    ]
    if basic:
        # Column buckets prevent row-wise interleaving of two-column PDFs.
        widths: dict[int, float] = {}
        for unit in texts:
            box = unit.get("bbox") or [0, 0, 0, 0]
            widths[unit["page"]] = max(widths.get(unit["page"], 0), box[2])
        texts.sort(
            key=lambda u: (
                u["page"],
                int(bool(u.get("bbox") and u["bbox"][0] > widths[u["page"]] * 0.48)),
                -(u.get("bbox") or [0, 0, 0, 0])[3],
            )
        )
    groups: list[list[dict[str, Any]]] = []
    for unit in texts:
        previous = groups[-1][-1] if groups else None
        append = (
            previous is not None
            and unit["page"] == previous["page"]
            and unit.get("kind") == previous.get("kind")
            and unit.get("heading_path", []) == previous.get("heading_path", [])
        )
        if previous is not None and append and basic:
            a, b = previous.get("bbox"), unit.get("bbox")
            append = bool(a and b and abs(a[0] - b[0]) < 35 and abs(a[1] - b[3]) < 22)
        if previous is not None and append and not basic:
            append = len(previous["text"]) < 180 and unit.get("kind") != "heading"
        if append and sum(len(u["text"]) for u in groups[-1]) + len(unit["text"]) <= limit:
            groups[-1].append(unit)
        else:
            groups.append([unit])
    return groups


def extract(library: DocumentLibrary, entries: list[Rendition], limit: int) -> list[Sample]:
    samples: list[Sample] = []
    # Prefer paragraph-aware Marker per source version; retain Basic-only sources.
    versions: dict[str, list[Rendition]] = {}
    for entry in entries:
        versions.setdefault(entry.version, []).append(entry)
    for version_entries in versions.values():
        preferred = next((e for e in version_entries if e.rendition != "basic"), version_entries[0])
        units = library.units(preferred)
        blocks = _blocks(units, preferred.rendition == "basic", limit)
        for group in blocks:
            text = "\n".join(u["text"] for u in group)
            sources = [preferred.locator("unit", u["id"]) for u in group]
            # Split long semantic units at sentence/word boundaries, retaining all locators.
            parts = []
            while len(text) > limit:
                stop = max(
                    text.rfind("。", 0, limit),
                    text.rfind(". ", 0, limit),
                    text.rfind(" ", 0, limit),
                )
                stop = stop + 1 if stop > limit // 2 else limit
                parts.append(text[:stop])
                text = text[stop:].lstrip()
            if text:
                parts.append(text)
            for n, part in enumerate(parts):
                samples.append(
                    Sample(
                        digest([sources, n])[:32],
                        "text_block",
                        preferred.doc_id,
                        preferred.version,
                        preferred.rendition,
                        sources[0],
                        preferred.title,
                        group[0]["page"],
                        text=part,
                        sources=sources,
                        bbox=group[0].get("bbox"),
                        heading_path=group[0].get("heading_path", []),
                    )
                )
        for entry in version_entries:
            entry_units = library.units(entry)
            associated = {u["id"]: u for u in entry_units}
            if entry.rendition != "basic":
                candidates = [
                    u for u in entry_units if u["kind"] in {"figure", "table"} and u.get("assets")
                ]
            else:
                # Page images are explicitly indexed as pages, never called figures.
                candidates = (
                    []
                    if any(e.rendition != "basic" for e in version_entries)
                    else [
                        {
                            "id": f"page_{p['page']}",
                            "kind": "page",
                            "page": p["page"],
                            "assets": [p["render_asset"]],
                            "caption": "",
                            "bbox": None,
                        }
                        for p in library.metadata(entry.path / "pages.json")["pages"]
                        if p.get("render_asset")
                    ]
                )
            for unit in candidates:
                for asset in unit["assets"]:
                    locator = (
                        entry.locator("page", unit["page"])
                        if unit["kind"] == "page"
                        else entry.locator("unit", unit["id"])
                    )
                    metadata = library.metadata(entry.path / "assets/index.json")["assets"][asset]
                    if not metadata.get("mime", "image/png").startswith("image/"):
                        continue
                    path = library.safe_path(entry, metadata["path"])
                    context = "\n".join(
                        u.get("text", "")
                        for u in associated.values()
                        if u["page"] == unit["page"]
                        and u["kind"] not in {"page", "figure", "table"}
                    )[:5000]
                    samples.append(
                        Sample(
                            digest(
                                [entry.doc_id, entry.version, entry.rendition, unit["id"], asset]
                            )[:32],
                            "image",
                            entry.doc_id,
                            entry.version,
                            entry.rendition,
                            locator,
                            entry.title,
                            unit["page"],
                            image=str(path),
                            asset_id=asset,
                            caption=unit.get("caption", "") or "",
                            context=context,
                            sources=[locator],
                            bbox=unit.get("bbox"),
                            image_kind=unit["kind"],
                            context_sources=[
                                entry.locator("unit", u["id"])
                                for u in associated.values()
                                if u["page"] == unit["page"]
                                and u["kind"] not in {"page", "figure", "table"}
                            ],
                        )
                    )
    return samples
