"""Stable, filesystem-safe identities for uploaded sources."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_BAD = re.compile(r"[^\w.\- ()\[\]\u4e00-\u9fff]+", re.UNICODE)


def sanitize_filename(name: str) -> str:
    """Return a display/storage basename; never a path."""
    base = Path(str(name).replace("\\", "/")).name.strip()
    base = _BAD.sub("_", base)
    base = base.strip(" .")
    if not base:
        raise ValueError("A filename is required")
    if len(base) > 180:
        stem, dot, suffix = base.rpartition(".")
        base = (stem[: 175 - len(suffix)] + dot + suffix) if dot else base[:180]
    return base


def doc_id_for_name(name: str) -> str:
    """Match the existing Poppler/basic pipeline's deterministic doc id."""
    normalized = sanitize_filename(name)
    return "paper_" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
