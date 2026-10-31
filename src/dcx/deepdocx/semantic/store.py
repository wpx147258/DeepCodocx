"""SQLite vector/metadata store; atomic document generations and reusable caches."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sqlite_vec  # type: ignore[import-untyped]

from .samples import file_hash

REPRESENTATIONS = (
    "text_block",
    "image",
    "caption_original",
    "caption_generated",
    "image_caption_original",
    "image_caption_generated",
)


class VectorStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS documents(doc_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, profile TEXT NOT NULL, updated REAL NOT NULL, complete INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS vectors(id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, fingerprint TEXT NOT NULL, profile TEXT NOT NULL, representation TEXT NOT NULL, space TEXT NOT NULL, vector BLOB NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS vectors_scope ON vectors(profile,doc_id,representation);
                CREATE TABLE IF NOT EXISTS embeddings(key TEXT PRIMARY KEY, vector BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS captions(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, stamp TEXT NOT NULL, hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def hash_file(self, conn: sqlite3.Connection, path: Path) -> str:
        stat = path.stat()
        stamp = json.dumps([stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])
        row = conn.execute("SELECT stamp,hash FROM files WHERE path=?", (str(path),)).fetchone()
        if row and row["stamp"] == stamp:
            return str(row["hash"])
        hashed = file_hash(path)
        conn.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (str(path), stamp, hashed))
        return hashed

    def cache(self, key: str, vector: list[float] | None = None) -> list[float] | None:
        import struct

        with self.connect() as conn:
            if vector is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings VALUES (?,?)",
                    (key, sqlite_vec.serialize_float32(vector)),
                )
                return vector
            row = conn.execute("SELECT vector FROM embeddings WHERE key=?", (key,)).fetchone()
            return list(struct.unpack("<" + "f" * (len(row[0]) // 4), row[0])) if row else None

    def caption(self, key: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        with self.connect() as conn:
            if payload is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO captions VALUES (?,?)",
                    (key, json.dumps(payload, ensure_ascii=False)),
                )
                return payload
            row = conn.execute("SELECT payload FROM captions WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def publish(
        self,
        doc_id: str,
        fingerprint: str,
        profile: str,
        records: list[dict[str, Any]],
        *,
        complete: bool = True,
    ) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM vectors WHERE doc_id=?", (doc_id,))
            for record in records:
                conn.execute(
                    "INSERT INTO vectors VALUES (?,?,?,?,?,?,?,?)",
                    (
                        record["id"],
                        doc_id,
                        fingerprint,
                        profile,
                        record["representation"],
                        record["space"],
                        sqlite_vec.serialize_float32(record["vector"]),
                        json.dumps(record["payload"], ensure_ascii=False),
                    ),
                )
            conn.execute(
                "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?)",
                (doc_id, fingerprint, profile, time.time(), int(complete)),
            )

    def prune(self, live: set[str]) -> None:
        with self.connect() as conn:
            for row in conn.execute("SELECT doc_id FROM documents").fetchall():
                if row[0] not in live:
                    conn.execute("DELETE FROM vectors WHERE doc_id=?", (row[0],))
                    conn.execute("DELETE FROM documents WHERE doc_id=?", (row[0],))

    def job(self, ident: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM jobs WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise ValueError("Unknown index job")
            return json.loads(row[0])

    def jobs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                json.loads(r[0])
                for r in conn.execute("SELECT payload FROM jobs ORDER BY rowid DESC LIMIT 30")
            ]

    def update_job(self, ident: str, **changes: Any) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM jobs WHERE id=?", (ident,)).fetchone()
            job = (
                json.loads(row[0])
                if row
                else {
                    "id": ident,
                    "created": time.time(),
                    "status": "queued",
                    "processed": 0,
                    "total": 0,
                    "error": "",
                    "errors": [],
                }
            )
            # Cancellation cannot be overwritten by a stale worker result.
            if job.get("status") == "cancelled" and changes.get("status") != "cancelled":
                changes.pop("status", None)
            job.update(changes)
            job["updated"] = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?,?)",
                (ident, json.dumps(job, ensure_ascii=False)),
            )
            return job

    def preference(self, key: str, value: Any = None) -> Any:
        with self.connect() as conn:
            if value is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO preferences VALUES (?,?)", (key, json.dumps(value))
                )
                return value
            row = conn.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def search(
        self,
        vector: list[float],
        profile: str,
        live: dict[str, str],
        representations: list[str],
        top_k: int,
        space: str = "gitee",
    ) -> list[dict[str, Any]]:
        if not live:
            return []
        with self.connect() as conn:
            conn.execute("CREATE TEMP TABLE valid_docs(doc_id TEXT PRIMARY KEY,fingerprint TEXT)")
            conn.executemany("INSERT INTO valid_docs VALUES (?,?)", live.items())
            sql = "SELECT v.payload,v.representation,vec_distance_cosine(v.vector,?) AS distance FROM vectors v JOIN valid_docs d ON v.doc_id=d.doc_id AND v.fingerprint=d.fingerprint WHERE v.profile=? AND v.space=?"
            params: list[Any] = [sqlite_vec.serialize_float32(vector), profile, space]
            if representations:
                sql += " AND v.representation IN (" + ",".join("?" for _ in representations) + ")"
                params.extend(representations)
            sql += " ORDER BY distance LIMIT ?"
            params.append(top_k * 6)
            rows = conn.execute(sql, params).fetchall()
            # Different representations of an asset support one source hit.
            found: dict[str, dict[str, Any]] = {}
            for row in rows:
                item = json.loads(row["payload"])
                score = 1 - float(row["distance"])
                source = item["locator"]
                key = item.get("sample_id") or source
                if key not in found:
                    found[key] = {
                        **item,
                        "score": score,
                        "matched_representations": [row["representation"]],
                    }
                else:
                    found[key]["matched_representations"].append(row["representation"])
            return list(found.values())[:top_k]
