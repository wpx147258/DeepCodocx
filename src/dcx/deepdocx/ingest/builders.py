"""Thin adapters around the two bundle builders."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .basic import convert as basic_convert
from .commands import run_command
from .marker_bundle import parse_marker, write_pdf_bundle
from .marker_bundle.parser import _render_pages


class BuildError(RuntimeError):
    pass


def build_basic(
    pdf: Path, out_root: Path, *, doc_id: str, dpi: int = 72,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    return basic_convert(pdf, out_root, dpi=dpi, doc_id=doc_id, cancelled=cancelled)


def _find_marker_json(directory: Path) -> Path:
    candidates = [p for p in directory.rglob("*.json") if not p.name.endswith("_meta.json")]
    if not candidates:
        raise BuildError("Marker produced no JSON output")
    return max(candidates, key=lambda p: p.stat().st_size)


def build_marker(
    pdf: Path,
    out_root: Path,
    doc_id: str,
    *,
    marker_command: str,
    timeout: int = 1800,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    command = shlex.split(marker_command)
    if not command:
        raise BuildError("Marker command is not configured")
    work = Path(tempfile.mkdtemp(prefix=".dcx-marker-", dir=str(out_root)))
    try:
        marker_dir = work / "json"
        marker_dir.mkdir()
        run_command(
            [
                *command,
                str(pdf),
                "--output_format",
                "json",
                "--output_dir",
                str(marker_dir),
                "--disable_multiprocessing",
                "--disable_tqdm",
            ],
            cwd=pdf.parent,
            timeout=timeout,
            cancelled=cancelled,
        )
        marker_json = _find_marker_json(marker_dir)
        parsed = parse_marker(
            marker_json, pdf, render=True, render_dir=work / "pages",
            page_renderer=lambda source, directory, dpi: _render_pages(source, directory, dpi, cancelled=cancelled),
        )
        produced = write_pdf_bundle(parsed, work / "bundle", doc_id=doc_id, source_version="v1")
        target = out_root / doc_id / "v1" / "marker"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(produced), str(target))
        return {
            "status": "built",
            "doc_id": doc_id,
            "pages": len(parsed.pages),
            "blocks": len(parsed.blocks),
            "bundle": str(target),
        }
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"Marker timed out after {timeout}s") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
