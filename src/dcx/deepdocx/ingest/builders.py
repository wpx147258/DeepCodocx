"""Thin adapters around the two bundle builders."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .basic import convert as basic_convert
from .marker_bundle import parse_marker, write_pdf_bundle


class BuildError(RuntimeError):
    pass


def build_basic(
    pdf: Path, out_root: Path, *, doc_id: str, dpi: int = 72
) -> dict[str, Any]:
    return basic_convert(pdf, out_root, dpi=dpi, doc_id=doc_id)


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
) -> dict[str, Any]:
    command = shlex.split(marker_command)
    if not command:
        raise BuildError("Marker command is not configured")
    work = Path(tempfile.mkdtemp(prefix=".dcx-marker-", dir=str(out_root)))
    try:
        marker_dir = work / "json"
        marker_dir.mkdir()
        proc = subprocess.run(
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
            cwd=str(pdf.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise BuildError((proc.stderr or proc.stdout or "Marker failed")[-1200:])
        marker_json = _find_marker_json(marker_dir)
        parsed = parse_marker(marker_json, pdf, render=True)
        produced = write_pdf_bundle(parsed, work / "bundle", doc_id=doc_id)
        target = out_root / doc_id / "v1" / "marker"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(produced), str(target))
        manifest_path = target / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source_version"] = "v1"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        units_path = target / "units.jsonl"
        if units_path.is_file():
            lines = []
            for line in units_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                unit = json.loads(line)
                unit["source_version"] = "v1"
                lines.append(json.dumps(unit, ensure_ascii=False))
            units_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
