"""Atomic SQLite FTS index for basic bundles."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from .control import locked
from .publish import notify_library


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS docs (
            doc_id TEXT NOT NULL,
            version TEXT NOT NULL,
            title TEXT NOT NULL,
            page_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (doc_id, version)
        );
        CREATE TABLE IF NOT EXISTS units (
            unit_key TEXT PRIMARY KEY,
            doc_id TEXT NOT NULL,
            version TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            page_start INTEGER NOT NULL,
            page_end INTEGER NOT NULL,
            bbox_json TEXT,
            asset_ids_json TEXT,
            locator TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
            unit_key UNINDEXED,
            text,
            labels,
            heading
        );
        """
    )


def build_basic_index(library_root: Path) -> dict[str, int]:
    """Administrative full rebuild, preserving the previous index on failure."""
    return update_basic_index(library_root, None)


def update_basic_index(library_root: Path, doc_ids: list[str] | None) -> dict[str, int]:
    """Update only affected Basic documents; bootstrap all if no index exists."""
    with locked(library_root / ".dcx-index.lock"):
        return _update(library_root, doc_ids)


def _update(library_root: Path, doc_ids: list[str] | None) -> dict[str, int]:
    target = library_root / "catalog.sqlite"
    incremental = target.is_file() and doc_ids is not None
    manifests = sorted(library_root.glob("*/v*/basic/manifest.json"))
    if incremental:
        manifests = [path for path in manifests if path.parents[2].name in (doc_ids or [])]
    temp = library_root.parent / f".catalog-{uuid.uuid4().hex}.sqlite"
    documents = units = 0
    try:
        with closing(sqlite3.connect(str(temp))) as connection:
            if incremental:
                with closing(
                    sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
                ) as existing:
                    existing.backup(connection)
            _schema(connection)
            if incremental:
                for doc_id in doc_ids or []:
                    connection.execute(
                        "DELETE FROM units_fts WHERE unit_key IN (SELECT unit_key FROM units WHERE doc_id=?)",
                        (doc_id,),
                    )
                    connection.execute("DELETE FROM units WHERE doc_id=?", (doc_id,))
                    connection.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))
            for manifest_path in manifests:
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    doc = manifest["doc"]
                except (OSError, json.JSONDecodeError, KeyError, TypeError):
                    raise ValueError(
                        f"Cannot index invalid Basic manifest: {manifest_path}"
                    ) from None
                doc_id = str(doc["doc_id"])
                version = str(doc.get("version") or "v1")
                title = str(doc.get("title") or doc_id)
                page_count = int(doc.get("page_count") or 0)
                status = str(doc.get("status") or "published")
                connection.execute(
                    "INSERT OR REPLACE INTO docs(doc_id,version,title,page_count,status) "
                    "VALUES (?,?,?,?,?)",
                    (doc_id, version, title, page_count, status),
                )
                documents += 1
                chunks_path = manifest_path.parent / "chunks.jsonl"
                if not chunks_path.is_file():
                    raise ValueError(f"Missing chunks: {chunks_path}")
                for line in chunks_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                        chunk_id = str(chunk["chunk_id"])
                    except (json.JSONDecodeError, KeyError, TypeError):
                        raise ValueError(f"Invalid chunk in {chunks_path}") from None
                    unit_key = f"{doc_id}:{version}:{chunk_id}"
                    page_range = chunk.get("page_range") or [1, 1]
                    if not isinstance(page_range, list) or len(page_range) != 2:
                        raise ValueError(f"Invalid page range in {chunks_path}")
                    bbox = chunk.get("bbox")
                    labels = " ".join(str(item) for item in chunk.get("labels", []))
                    heading = " ".join(str(item) for item in chunk.get("heading_path", []))
                    connection.execute(
                        "INSERT OR REPLACE INTO units("
                        "unit_key,doc_id,version,chunk_id,kind,text,page_start,page_end,"
                        "bbox_json,asset_ids_json,locator) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            unit_key,
                            doc_id,
                            version,
                            chunk_id,
                            str(chunk.get("kind") or "text"),
                            str(chunk.get("text") or ""),
                            int(page_range[0]),
                            int(page_range[1]),
                            json.dumps(bbox) if bbox else None,
                            json.dumps(list(chunk.get("asset_ids", []))),
                            f"doc:{doc_id}@{version}/basic/unit:{chunk_id}",
                        ),
                    )
                    connection.execute(
                        "INSERT INTO units_fts(unit_key,text,labels,heading) VALUES (?,?,?,?)",
                        (unit_key, str(chunk.get("text") or ""), labels, heading),
                    )
                    units += 1
            connection.commit()
        os.replace(temp, target)
        notify_library(library_root)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    return {"documents": documents, "units": units}
