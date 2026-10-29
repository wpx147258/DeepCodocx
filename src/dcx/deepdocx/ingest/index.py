"""Atomic SQLite FTS index for basic bundles."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path


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
    """Rebuild catalog.sqlite from all basic renditions atomically."""
    manifests = sorted(library_root.glob("*/v*/basic/manifest.json"))
    if not manifests:
        return {"documents": 0, "units": 0}
    temp = library_root.parent / f".catalog-{uuid.uuid4().hex}.sqlite"
    documents = units = 0
    try:
        with sqlite3.connect(str(temp)) as connection:
            _schema(connection)
            for manifest_path in manifests:
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    doc = manifest["doc"]
                except (OSError, json.JSONDecodeError, KeyError, TypeError):
                    continue
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
                    continue
                for line in chunks_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                        chunk_id = str(chunk["chunk_id"])
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
                    unit_key = f"{doc_id}:{version}:{chunk_id}"
                    page_range = chunk.get("page_range") or [1, 1]
                    assert isinstance(page_range, list) and len(page_range) >= 2
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
        os.replace(temp, library_root / "catalog.sqlite")
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    return {"documents": documents, "units": units}
