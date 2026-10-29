"""Tiny heterogeneous corpus; no real documents or network required."""

from __future__ import annotations

import base64
import json
from pathlib import Path

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aEl8AAAAASUVORK5CYII="
)


def document_library(root: Path) -> Path:
    for rendition in ("basic", "marker"):
        path = root / "paper_alpha" / "v1" / rendition
        (path / "assets").mkdir(parents=True)
        (path / "assets" / "page.png").write_bytes(PNG)

        def write(name, value, path=path):
            (path / name).write_text(json.dumps(value), encoding="utf-8")

        write(
            "pages.json",
            {"pages": [{"page": 1, "width": 612, "height": 792, "render_asset": "as_page_0001"}]},
        )
        write(
            "assets/index.json",
            {"assets": {"as_page_0001": {"path": "assets/page.png", "mime": "image/png"}}},
        )
        if rendition == "basic":
            write(
                "manifest.json",
                {
                    "schema": "dda.bundle/1",
                    "doc": {
                        "doc_id": "paper_alpha",
                        "version": "v1",
                        "title": "Alpha Memory",
                        "page_count": 1,
                    },
                },
            )
            write("document.json", {"blocks": []})
            units = [
                {
                    "chunk_id": f"c_{i}",
                    "kind": "text",
                    "text": text,
                    "page_range": [1, 1],
                    "bbox": [0, 0, 100, 20],
                }
                for i, text in enumerate(
                    ["Memory stores visual tokens.", "A retrieved token conditions the action."]
                )
            ]
            (path / "chunks.jsonl").write_text("\n".join(json.dumps(unit) for unit in units))
        else:
            write(
                "manifest.json",
                {
                    "schema": "dda.bundle/3",
                    "doc_id": "paper_alpha",
                    "source_version": "v1",
                    "rendition_id": "marker-json-v1",
                },
            )
            write("source.json", {"original_name": "Alpha Memory.pdf"})
            write("content.json", {"blocks": []})
            units = [
                {
                    "unit_id": "u_paragraph",
                    "unit_type": "paragraph",
                    "page": 1,
                    "bbox": [0, 0, 100, 50],
                    "text": {
                        "body": "Memory stores complete semantic observations for later action."
                    },
                },
                {
                    "unit_id": "u_figure",
                    "unit_type": "figure",
                    "page": 1,
                    "bbox": [0, 50, 100, 100],
                    "text": {"caption": "Figure 1: memory architecture"},
                    "assets": {"images": ["as_page_0001"]},
                },
            ]
            (path / "units.jsonl").write_text("\n".join(json.dumps(unit) for unit in units))
    return root
