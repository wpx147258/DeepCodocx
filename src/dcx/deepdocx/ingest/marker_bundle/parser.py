"""Marker JSON -> normalized PDF document for Bundle v3.

This module is independent of the legacy Poppler/Bundle-v2 code.
"""

from __future__ import annotations

import html
import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TAG = re.compile(r"<[^>]+>")
CONTENT_REF = re.compile(r"<content-ref[^>]*>.*?</content-ref>", re.DOTALL)
CAPTION_LABEL = re.compile(r"^(Figure|Table)\s+([0-9]+[a-z]?)", re.IGNORECASE)
PAGE_ID = re.compile(r"/page/(\d+)/")


@dataclass(frozen=True)
class Page:
    page: int
    width: float
    height: float
    marker_id: str


@dataclass
class Block:
    block_id: str
    kind: str
    page: int
    bbox: tuple[float, float, float, float]
    text: str
    html: str
    section_path: tuple[str, ...]
    level: int = 0
    label: str | None = None
    caption: str | None = None
    rows: tuple[tuple[str, ...], ...] = ()
    items: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()


@dataclass
class ParsedPdf:
    path: Path
    source_sha256: str
    pages: list[Page]
    blocks: list[Block]
    assets: dict[str, Path | None]
    warnings: list[str] = field(default_factory=list)
    presentation: str | None = None
    page_images: dict[int, Path] = field(default_factory=dict)


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plain_html(value: str) -> str:
    value = CONTENT_REF.sub(" ", value)
    value = html.unescape(value)
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    value = TAG.sub(" ", value)
    return " ".join(html.unescape(value).split())


def _page_number(node: dict[str, Any]) -> int | None:
    match = PAGE_ID.search(str(node.get("id", "")))
    return int(match.group(1)) + 1 if match else None


def _page_box(node: dict[str, Any]) -> tuple[float, float, float, float]:
    values = node.get("bbox")
    if not isinstance(values, list) or len(values) != 4:
        return 0.0, 0.0, 1.0, 1.0
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def _to_pdf_box(box: tuple[float, ...], height: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    return (float(x0), float(height - y1), float(x1), float(height - y0))


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    children = node.get("children")
    return children if isinstance(children, list) else []


def _table_rows(value: str) -> tuple[tuple[str, ...], ...]:
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", value, re.DOTALL | re.IGNORECASE):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL | re.IGNORECASE)
        if cells:
            rows.append(tuple(plain_html(cell) for cell in cells))
    return tuple(rows)


def _section_titles(document: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}

    def walk(node: dict[str, Any]) -> None:
        if node.get("block_type") == "SectionHeader":
            result[str(node.get("id"))] = plain_html(str(node.get("html", "")))
        for child in _children(node):
            walk(child)

    walk(document)
    return result


def _path_for(
    node: dict[str, Any], page: int, titles: dict[str, str], pages: dict[int, Page]
) -> tuple[str, ...]:
    hierarchy = node.get("section_hierarchy")
    if not isinstance(hierarchy, dict):
        return ()
    values = []
    for key in sorted(hierarchy, key=lambda item: int(item) if str(item).isdigit() else 0):
        title = titles.get(str(hierarchy[key]))
        if title:
            values.append(title)
    return tuple(values)


def _render_pages(
    pdf_path: Path, work_dir: Path, dpi: int, *, cancelled: Callable[[], bool] | None = None,
) -> dict[int, Path]:
    from ..commands import run_command
    work_dir.mkdir(parents=True, exist_ok=True)
    prefix = work_dir / "page"
    run_command(
        [
            "pdftoppm",
            "-png",
            "-r",
            str(dpi),
            str(pdf_path),
            str(prefix),
        ],
        timeout=300,
        cancelled=cancelled,
    )
    result = {}
    for path in sorted(work_dir.glob("page-*.png")):
        match = re.search(r"page-(\d+)\.png$", path.name)
        if match:
            result[int(match.group(1))] = path
    return result


def _crop(
    page_image: Path,
    box: tuple[float, float, float, float],
    out: Path,
    page_width: float,
    page_height: float,
) -> None:
    from PIL import Image

    image = Image.open(page_image).convert("RGB")
    width, height = image.size
    x0, y0, x1, y1 = box
    sx = width / page_width
    sy = height / page_height
    image.crop(
        (
            round(x0 * sx),
            round((page_height - y1) * sy),
            round(x1 * sx),
            round((page_height - y0) * sy),
        )
    ).save(out)


def parse_marker(
    marker_json: Path,
    pdf_path: Path,
    *,
    presentation: Path | None = None,
    render: bool = False,
    render_dir: Path | None = None,
    page_renderer: Callable[[Path, Path, int], dict[int, Path]] = _render_pages,
) -> ParsedPdf:
    if not pdf_path.is_file():
        raise ValueError(f"PDF is missing: {pdf_path}")
    document = json.loads(marker_json.read_text(encoding="utf-8"))
    if document.get("block_type") != "Document":
        raise ValueError("Marker JSON root must be a Document")
    pages: list[Page] = []
    page_nodes = [child for child in _children(document) if child.get("block_type") == "Page"]
    for node in sorted(page_nodes, key=lambda item: _page_number(item) or 0):
        page_number = _page_number(node)
        if page_number is None:
            continue
        box = _page_box(node)
        pages.append(
            Page(
                page=page_number,
                width=box[2] - box[0],
                height=box[3] - box[1],
                marker_id=str(node.get("id")),
            )
        )
    pages.sort(key=lambda item: item.page)
    page_by_number = {page.page: page for page in pages}
    titles = _section_titles(document)
    blocks: list[Block] = []
    assets: dict[str, Path | None] = {}
    warnings: list[str] = []
    render_root = render_dir or Path(tempfile.mkdtemp(prefix="marker-pages-"))
    render_root.mkdir(parents=True, exist_ok=True)
    rendered = page_renderer(pdf_path, render_root, 150) if render else {}
    counter = 0

    def add(
        node: dict[str, Any],
        kind: str,
        page: int,
        box: tuple[float, float, float, float],
        text: str,
        *,
        level: int = 0,
        label: str | None = None,
        caption: str | None = None,
        rows: tuple[tuple[str, ...], ...] = (),
        items: tuple[str, ...] = (),
        asset_ids: tuple[str, ...] = (),
    ) -> Block:
        nonlocal counter
        counter += 1
        block = Block(
            block_id=f"pb_{counter:06d}",
            kind=kind,
            page=page,
            bbox=box,
            text=text,
            html=str(node.get("html", "")),
            section_path=_path_for(node, page, titles, page_by_number),
            level=level,
            label=label,
            caption=caption,
            rows=rows,
            items=items,
            asset_ids=asset_ids,
        )
        blocks.append(block)
        return block

    def visit(node: dict[str, Any], page: int) -> None:
        kind = node.get("block_type")
        if kind in {"PageHeader", "PageFooter"}:
            return
        if kind == "TableGroup":
            caption = next((c for c in _children(node) if c.get("block_type") == "Caption"), None)
            table = next((c for c in _children(node) if c.get("block_type") == "Table"), None)
            if table is None:
                warnings.append(f"table group without table on page {page}")
                return
            box = _to_pdf_box(_page_box(table), page_by_number[page].height)
            table_html = str(table.get("html", ""))
            caption_text = plain_html(str(caption.get("html", ""))) if caption else ""
            match = CAPTION_LABEL.match(caption_text)
            label = f"{match.group(1).title()} {match.group(2)}" if match else None
            asset_id = f"as_tab_{len(assets) + 1:06d}"
            asset_path = None
            page_image = rendered.get(page)
            if page_image is not None:
                crop_path = render_root / f"{asset_id}.png"
                try:
                    page_info = page_by_number[page]
                    _crop(page_image, box, crop_path, page_info.width, page_info.height)
                    asset_path = crop_path
                except Exception as exc:  # pragma: no cover
                    warnings.append(f"table crop failed: {exc}")
            if asset_path is not None:
                assets[asset_id] = asset_path
            add(
                table,
                "table",
                page,
                box,
                plain_html(table_html),
                label=label,
                caption=caption_text or None,
                rows=_table_rows(table_html),
                asset_ids=(asset_id,) if asset_path is not None else (),
            )
            return
        if kind == "FigureGroup":
            figure = next((c for c in _children(node) if c.get("block_type") == "Figure"), None)
            caption = next((c for c in _children(node) if c.get("block_type") == "Caption"), None)
            if figure is None:
                warnings.append(f"figure group without figure on page {page}")
                return
            box = _to_pdf_box(_page_box(figure), page_by_number[page].height)
            caption_text = plain_html(str(caption.get("html", ""))) if caption else ""
            match = CAPTION_LABEL.match(caption_text)
            label = f"{match.group(1).title()} {match.group(2)}" if match else None
            asset_id = f"as_fig_{len(assets) + 1:06d}"
            asset_path = None
            page_image = rendered.get(page)
            if page_image is not None:
                crop_path = render_root / f"{asset_id}.png"
                try:
                    page_info = page_by_number[page]
                    _crop(page_image, box, crop_path, page_info.width, page_info.height)
                    asset_path = crop_path
                except Exception as exc:  # pragma: no cover - optional Pillow failure
                    warnings.append(f"figure crop failed: {exc}")
            if asset_path is not None:
                assets[asset_id] = asset_path
            add(
                figure,
                "figure",
                page,
                box,
                caption_text,
                label=label,
                caption=caption_text or None,
                asset_ids=(asset_id,) if asset_path is not None else (),
            )
            return
        if kind == "PictureGroup":
            caption = next((c for c in _children(node) if c.get("block_type") == "Caption"), None)
            pictures = [c for c in _children(node) if c.get("block_type") == "Picture"]
            if not pictures:
                warnings.append(f"picture group without picture on page {page}")
                return
            box = _to_pdf_box(_page_box(node), page_by_number[page].height)
            caption_text = plain_html(str(caption.get("html", ""))) if caption else ""
            match = CAPTION_LABEL.match(caption_text)
            label = f"{match.group(1).title()} {match.group(2)}" if match else None
            asset_id = f"as_fig_{len(assets) + 1:06d}"
            asset_path = None
            page_image = rendered.get(page)
            if page_image is not None:
                crop_path = render_root / f"{asset_id}.png"
                try:
                    page_info = page_by_number[page]
                    _crop(page_image, box, crop_path, page_info.width, page_info.height)
                    asset_path = crop_path
                except Exception as exc:  # pragma: no cover
                    warnings.append(f"picture crop failed: {exc}")
            if asset_path is not None:
                assets[asset_id] = asset_path
            add(
                node,
                "figure",
                page,
                box,
                caption_text,
                label=label,
                caption=caption_text or None,
                asset_ids=(asset_id,) if asset_path is not None else (),
            )
            return
        if kind == "ListGroup":
            items = []
            for item in _children(node):
                if item.get("block_type") == "ListItem":
                    items.append(plain_html(str(item.get("html", ""))))
            box = _to_pdf_box(_page_box(node), page_by_number[page].height)
            add(node, "list", page, box, " ".join(items), items=tuple(items))
            return
        if kind == "SectionHeader":
            level_match = re.search(r"<h([1-6])", str(node.get("html", "")), re.IGNORECASE)
            level = int(level_match.group(1)) if level_match else 1
            text = plain_html(str(node.get("html", "")))
            add(
                node,
                "heading",
                page,
                _to_pdf_box(_page_box(node), page_by_number[page].height),
                text,
                level=level,
            )
            return
        if kind in {"Text", "Caption", "Footnote", "Reference", "Code"}:
            text = plain_html(str(node.get("html", "")))
            if not text:
                return
            unit_kind = {
                "Text": "paragraph",
                "Caption": "caption",
                "Footnote": "footnote",
                "Reference": "reference",
                "Code": "code",
            }[kind]
            add(
                node,
                unit_kind,
                page,
                _to_pdf_box(_page_box(node), page_by_number[page].height),
                text,
            )
            return
        if kind == "Table":
            box = _to_pdf_box(_page_box(node), page_by_number[page].height)
            table_html = str(node.get("html", ""))
            asset_id = f"as_tab_{len(assets) + 1:06d}"
            asset_path = None
            page_image = rendered.get(page)
            if page_image is not None:
                crop_path = render_root / f"{asset_id}.png"
                try:
                    page_info = page_by_number[page]
                    _crop(page_image, box, crop_path, page_info.width, page_info.height)
                    asset_path = crop_path
                except Exception as exc:  # pragma: no cover
                    warnings.append(f"table crop failed: {exc}")
            if asset_path is not None:
                assets[asset_id] = asset_path
            add(
                node,
                "table",
                page,
                box,
                plain_html(table_html),
                rows=_table_rows(table_html),
                asset_ids=(asset_id,) if asset_path is not None else (),
            )
            return
        if kind == "Figure":
            box = _to_pdf_box(_page_box(node), page_by_number[page].height)
            asset_id = f"as_fig_{len(assets) + 1:06d}"
            asset_path = None
            page_image = rendered.get(page)
            if page_image is not None:
                crop_path = render_root / f"{asset_id}.png"
                try:
                    page_info = page_by_number[page]
                    _crop(page_image, box, crop_path, page_info.width, page_info.height)
                    asset_path = crop_path
                except Exception as exc:  # pragma: no cover
                    warnings.append(f"figure crop failed: {exc}")
            if asset_path is not None:
                assets[asset_id] = asset_path
            add(
                node,
                "figure",
                page,
                box,
                "",
                caption=None,
                asset_ids=(asset_id,) if asset_path is not None else (),
            )
            warnings.append(f"uncaptioned figure on page {page}")
            return
        if kind == "Equation":
            text = plain_html(str(node.get("html", "")))
            if text:
                add(
                    node,
                    "equation",
                    page,
                    _to_pdf_box(_page_box(node), page_by_number[page].height),
                    text,
                )
            return
        if kind == "Picture":
            box = _to_pdf_box(_page_box(node), page_by_number[page].height)
            asset_id = f"as_fig_{len(assets) + 1:06d}"
            asset_path = None
            page_image = rendered.get(page)
            if page_image is not None:
                crop_path = render_root / f"{asset_id}.png"
                try:
                    page_info = page_by_number[page]
                    _crop(page_image, box, crop_path, page_info.width, page_info.height)
                    asset_path = crop_path
                except Exception as exc:  # pragma: no cover
                    warnings.append(f"picture crop failed: {exc}")
            assets[asset_id] = asset_path
            add(node, "figure", page, box, "", asset_ids=(asset_id,))
            warnings.append(f"uncaptioned picture on page {page}")
            return
        for child in _children(node):
            visit(child, page)

    for node in page_nodes:
        page = _page_number(node)
        if page is None:
            continue
        for child in _children(node):
            visit(child, page)

    page_blocks = blocks
    presentation_text = None
    if presentation is not None:
        presentation_text = presentation.read_text(encoding="utf-8")
    return ParsedPdf(
        path=pdf_path,
        source_sha256=sha256_file(pdf_path),
        pages=pages,
        blocks=page_blocks,
        assets=assets,
        warnings=warnings,
        presentation=presentation_text,
        page_images=rendered,
    )
