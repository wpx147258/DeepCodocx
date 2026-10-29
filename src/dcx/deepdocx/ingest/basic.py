#!/usr/bin/env python
"""Convert PDFs into minimal dda.bundle/1 bundles using Poppler.

This is a basic text+page-image bundle: page/line bboxes, page PNGs, chunks.
Figures/tables are intentionally empty; enrich them later.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


@dataclass
class Page:
    number: int
    width: float
    height: float
    words: list[Word] = field(default_factory=list)


class _BBoxParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.pages: list[Page] = []
        self._page: Page | None = None
        self._word: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "page":
            self._page = Page(
                number=len(self.pages) + 1,
                width=float(values.get("width") or 612),
                height=float(values.get("height") or 792),
            )
            self.pages.append(self._page)
        elif tag == "word" and self._page is not None:
            self._word = {
                "x0": float(values.get("xmin") or values.get("xMin") or 0),
                "y0": float(values.get("ymin") or values.get("yMin") or 0),
                "x1": float(values.get("xmax") or values.get("xMax") or 0),
                "y1": float(values.get("ymax") or values.get("yMax") or 0),
                "text": "",
            }

    def handle_data(self, data: str) -> None:
        if self._word is not None:
            self._word["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "word" and self._word is not None and self._page is not None:
            text = " ".join(html.unescape(self._word["text"]).split())
            if text:
                self._page.words.append(Word(**{**self._word, "text": text}))
            self._word = None
        elif tag == "page":
            self._page = None


def _run(command: list[str], *, timeout: int = 300) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{command[0]} failed: {result.stderr.strip()[:500]}")
    return result.stdout


def _pages_from_pdf(path: Path) -> list[Page]:
    parser = _BBoxParser()
    parser.feed(_run(["pdftotext", "-bbox", "-enc", "UTF-8", str(path), "-"]))
    return parser.pages


def _line_blocks(page: Page) -> list[dict[str, Any]]:
    if not page.words:
        return [
            {
                "block_id": f"b_{page.number:04d}_000",
                "type": "paragraph",
                "page": page.number,
                "bbox": [0.0, 0.0, page.width, page.height],
                "text": "",
                "heading_path": [],
            }
        ]
    groups: list[list[Word]] = []
    for word in sorted(page.words, key=lambda item: (item.y0, item.x0)):
        if groups and abs(groups[-1][-1].y0 - word.y0) <= 2.0:
            groups[-1].append(word)
        else:
            groups.append([word])
    blocks: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        x0 = min(word.x0 for word in group)
        y0 = min(word.y0 for word in group)
        x1 = max(word.x1 for word in group)
        y1 = max(word.y1 for word in group)
        text = " ".join(word.text for word in group)
        blocks.append(
            {
                "block_id": f"b_{page.number:04d}_{index:03d}",
                "type": "paragraph",
                "page": page.number,
                "bbox": [
                    round(x0, 2),
                    round(max(0.0, page.height - y1), 2),
                    round(x1, 2),
                    round(max(0.0, page.height - y0), 2),
                ],
                "text": text,
                "heading_path": [],
            }
        )
    return blocks


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def convert(
    pdf: Path, out_root: Path, *, dpi: int = 72, doc_id: str | None = None
) -> dict[str, Any]:
    doc_id = doc_id or f"paper_{hashlib.sha1(pdf.name.encode()).hexdigest()[:12]}"
    target = out_root / doc_id / "v1" / "basic"
    if target.is_dir() and (target / "manifest.json").is_file():
        return {"pdf": pdf.name, "doc_id": doc_id, "status": "skipped", "bundle": str(target)}
    pages = _pages_from_pdf(pdf)
    if not pages:
        raise RuntimeError("no pages from pdftotext")
    staging_parent = out_root / doc_id
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".basic-", dir=staging_parent))
    try:
        (staging / "assets" / "pages").mkdir(parents=True)
        _run(
            [
                "pdftoppm",
                "-png",
                "-r",
                str(dpi),
                str(pdf),
                str(staging / "assets" / "pages" / "page"),
            ]
        )
        page_images = {
            int(path.stem.split("-")[-1]): path
            for path in (staging / "assets" / "pages").glob("page-*.png")
        }
        assets: dict[str, Any] = {}
        for page in pages:
            source = page_images.get(page.number)
            if source is None:
                continue
            asset_id = f"as_page_{page.number:04d}"
            relative = f"assets/pages/{source.name}"
            assets[asset_id] = {"path": relative, "mime": "image/png"}
        blocks: list[dict[str, Any]] = []
        chunks: list[dict[str, Any]] = []
        for page in pages:
            page_blocks = _line_blocks(page)
            blocks.extend(page_blocks)
            for block in page_blocks:
                if not block["text"]:
                    continue
                chunks.append(
                    {
                        "chunk_id": f"c_{page.number:04d}_{len(chunks):04d}",
                        "kind": "text",
                        "block_ids": [block["block_id"]],
                        "text": block["text"],
                        "page_range": [page.number, page.number],
                        "bbox": block["bbox"],
                        "heading_path": [],
                        "labels": [],
                        "asset_ids": [],
                    }
                )
        quality = {
            "score": 0.6 if chunks else 0.3,
            "flags": [] if chunks else [
                {"code": "NO_TEXT", "severity": "warning", "message": "no extractable text"}
            ],
            "checked_at": datetime.now(UTC).isoformat(),
        }
        title = re.sub(r"[_-]+", " ", pdf.stem).strip()
        manifest = {
            "schema": "dda.bundle/1",
            "as_of": datetime.now(UTC).isoformat(),
            "doc": {
                "doc_id": doc_id,
                "version": "v1",
                "title": title,
                "page_count": len(pages),
                "status": "published",
                "quality": quality,
            },
            "assets": assets,
            "checksums": {},
        }
        _write_json(staging / "manifest.json", manifest)
        _write_json(staging / "document.json", {"blocks": blocks})
        _write_json(staging / "figures.json", {"figures": []})
        _write_json(staging / "tables.json", {"tables": []})
        _write_json(
            staging / "pages.json",
            {
                "pages": [
                    {
                        "page": page.number,
                        "width": page.width,
                        "height": page.height,
                        "render_asset": f"as_page_{page.number:04d}",
                    }
                    for page in pages
                ]
            },
        )
        _write_json(
            staging / "assets" / "index.json",
            {"schema": "dda.assets/1", "assets": assets},
        )
        _write_json(
            staging / "source.json",
            {
                "schema": "dda.source/1",
                "format": "pdf",
                "page_model": "pdf",
                "source_sha256": _sha256(pdf),
                "original_name": pdf.name,
                "title": title,
            },
        )
        with (staging / "chunks.jsonl").open("w", encoding="utf-8") as handle:
            for chunk in chunks:
                handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        return {
            "pdf": pdf.name,
            "doc_id": doc_id,
            "status": "converted",
            "pages": len(pages),
            "chunks": len(chunks),
            "bundle": str(target),
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=72)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    pdfs = sorted(args.root.glob("*.pdf"))
    if args.limit:
        pdfs = pdfs[: args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    report: list[dict[str, Any]] = []
    indexed = list(enumerate(pdfs, start=1))

    def work(item: tuple[int, Path]) -> dict[str, Any]:
        index, pdf = item
        try:
            result = convert(pdf, args.out, dpi=args.dpi)
            result["status"] = result.get("status", "converted")
        except Exception as exc:  # noqa: BLE001 - per-file failure is data
            result = {"pdf": pdf.name, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        result["index"] = index
        return result

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {executor.submit(work, item): item for item in indexed}
            for future in as_completed(futures):
                result = future.result()
                report.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
    else:
        for item in indexed:
            result = work(item)
            report.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    report.sort(key=lambda item: item["index"])
    (args.out / "ingest-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"total": len(report), "converted": sum(r["status"] == "converted" for r in report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
