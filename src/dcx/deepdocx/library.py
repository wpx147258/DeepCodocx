"""Read-only basic/Marker bundle adapter and stable, rendition-aware evidence."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..atomic import atomic_write_text
from ..session.lock import FileLock

MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_LOCATOR = re.compile(
    r"^doc:([A-Za-z0-9_.-]+)@([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(page|unit|asset):([A-Za-z0-9_.-]+)$"
)


def read_json(path: Path) -> Any:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"Document metadata exceeds {MAX_JSON_BYTES} bytes")
    return json.loads(path.read_text(encoding="utf-8"))


def library_root(raw: str | Path) -> Path:
    root = Path(raw).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Library root must be a directory")
    if (root / "paper-bundles").is_dir():
        root = (root / "paper-bundles").resolve()
    return root


def library_id(root: Path) -> str:
    return "lib_" + hashlib.sha256(str(root).encode()).hexdigest()[:12]


class LibraryStore:
    def __init__(
        self,
        data_dir: Path,
        configured: tuple[str, ...] = (),
        *,
        default_root: str | None = None,
        fallback_roots: tuple[str, ...] = (),
    ) -> None:
        self.path = data_dir / "libraries.json"
        self.configured = configured
        self.default_root = default_root
        self.fallback_roots = fallback_roots
        self._fallback: dict[str, str] | None = None

    def _read(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.path.is_file():
            with contextlib.suppress(OSError, ValueError):
                data = read_json(self.path)
        return data if isinstance(data, dict) else {}

    def entries(self) -> list[dict[str, str]]:
        entries = self._read().get("libraries", [])
        result: dict[str, dict[str, str]] = {}
        for item in entries if isinstance(entries, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("root"), str):
                continue
            try:
                root = library_root(item["root"])
                ident = library_id(root)
                result[ident] = {
                    "id": ident,
                    "root": str(root),
                    "name": str(item.get("name") or root.name),
                }
            except (OSError, ValueError):
                continue
        for raw in (self.default_root, *self.configured):
            if not raw:
                continue
            try:
                root = library_root(raw)
                result.setdefault(
                    library_id(root),
                    {
                        "id": library_id(root),
                        "root": str(root),
                        "name": root.parent.name if root.name == "paper-bundles" else root.name,
                    },
                )
            except (OSError, ValueError):
                continue
        if not result:
            if self._fallback is not None and Path(self._fallback["root"]).is_dir():
                result[self._fallback["id"]] = self._fallback
            else:
                for raw in self.fallback_roots:
                    try:
                        root = library_root(raw)
                        if not DocumentLibrary(root).documents():
                            continue
                        self._fallback = {
                            "id": library_id(root),
                            "root": str(root),
                            "name": root.parent.name if root.name == "paper-bundles" else root.name,
                        }
                        result[self._fallback["id"]] = self._fallback
                        break
                    except (OSError, ValueError):
                        continue
        return list(result.values())

    def selected_id(self) -> str:
        entries = self.entries()
        selected = self._read().get("selected_library_id")
        if any(entry["id"] == selected for entry in entries):
            return str(selected)
        if self.default_root:
            with contextlib.suppress(OSError, ValueError):
                ident = library_id(library_root(self.default_root))
                if any(entry["id"] == ident for entry in entries):
                    return ident
        return entries[0]["id"] if entries else ""

    def select(self, ident: str) -> None:
        self.get(ident)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.path.with_suffix(".lock")):
            data = self._read()
            atomic_write_text(
                self.path,
                json.dumps(
                    {**data, "v": 1, "libraries": self.entries(), "selected_library_id": ident},
                    ensure_ascii=False,
                ),
            )

    def register(self, raw: str, name: str = "") -> dict[str, str]:
        root = library_root(raw)
        catalog = DocumentLibrary(root)
        if not catalog.documents():
            raise ValueError("No complete basic or Marker bundles were found in this directory")
        entry = {
            "id": library_id(root),
            "root": str(root),
            "name": name.strip()[:120]
            or (root.parent.name if root.name == "paper-bundles" else root.name),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.path.with_suffix(".lock")):
            data = self._read()
            entries = {item["id"]: item for item in self.entries()}
            entries[entry["id"]] = entry
            atomic_write_text(
                self.path,
                json.dumps(
                    {**data, "v": 1, "libraries": list(entries.values())}, ensure_ascii=False
                ),
            )
        return entry

    def get(self, ident: str) -> dict[str, str]:
        for entry in self.entries():
            if entry["id"] == ident:
                return entry
        raise ValueError("Unknown document library; select or register it first")

    def open(self, ident: str) -> DocumentLibrary:
        return DocumentLibrary(Path(self.get(ident)["root"]))


@dataclass(frozen=True)
class Rendition:
    doc_id: str
    version: str
    rendition: str
    title: str
    path: Path
    page_count: int

    def locator(self, kind: str, ident: str | int) -> str:
        return f"doc:{self.doc_id}@{self.version}/{self.rendition}/{kind}:{ident}"


class DocumentLibrary:
    def __init__(self, root: Path, documents: tuple[str, ...] = ()) -> None:
        self.root = library_root(root)
        self.id = library_id(self.root)
        self.scope = documents
        self._entries: dict[tuple[str, str, str], Rendition] | None = None
        self._units_cache: dict[Path, list[dict[str, Any]]] = {}
        self._revision: str | None = None

    def reload(self) -> None:
        """Drop the in-memory manifest/unit caches after a build completes."""
        self._entries = None
        self._units_cache.clear()

    def safe_path(self, entry: Rendition, relative: str) -> Path:
        path = (entry.path / relative).resolve(strict=True)
        if (
            not path.is_relative_to(entry.path)
            or not path.is_relative_to(self.root)
            or not path.is_file()
        ):
            raise ValueError("Asset is outside the selected bundle")
        return path

    def metadata(self, path: Path) -> Any:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ValueError("Metadata is outside the selected library")
        return read_json(resolved)

    def entries(self) -> list[Rendition]:
        revision_path = self.root / ".dcx-revision"
        try:
            revision = revision_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            revision = None
        if revision != self._revision:
            self.reload()
            self._revision = revision
        if self._entries is None:
            self._entries = {}
            manifests = (
                [self.root / "manifest.json"]
                if (self.root / "manifest.json").is_file()
                else sorted(self.root.rglob("manifest.json"))
            )
            for path in manifests[:10000]:
                try:
                    if not path.resolve().is_relative_to(self.root):
                        continue
                    raw = self.metadata(path)
                    if not isinstance(raw, dict):
                        continue
                    schema = raw.get("schema")
                    if schema == "dda.bundle/1":
                        doc = raw["doc"]
                        doc_id, version, rendition = (
                            str(doc["doc_id"]),
                            str(doc["version"]),
                            "basic",
                        )
                        title, page_count = (
                            str(doc.get("title") or doc_id),
                            int(doc.get("page_count") or 0),
                        )
                        required = (
                            "document.json",
                            "chunks.jsonl",
                            "pages.json",
                            "assets/index.json",
                        )
                    elif schema == "dda.bundle/3":
                        doc_id, version = str(raw["doc_id"]), str(raw["source_version"])
                        rendition = str(raw.get("rendition_id") or "marker-json-v1")
                        source = self.metadata(path.parent / "source.json")
                        title = str(source.get("title") or source.get("original_name") or doc_id)
                        page_count = len(self.metadata(path.parent / "pages.json").get("pages", []))
                        required = (
                            "content.json",
                            "units.jsonl",
                            "pages.json",
                            "assets/index.json",
                        )
                    else:
                        continue
                    if not all(_PART.fullmatch(part) for part in (doc_id, version, rendition)):
                        continue
                    if self.scope and doc_id not in self.scope:
                        continue
                    entry = Rendition(
                        doc_id, version, rendition, title, path.parent.resolve(), page_count
                    )
                    for name in required:
                        self.safe_path(entry, name)
                    self._entries[(doc_id, version, rendition)] = entry
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return list(self._entries.values())

    def documents(self, query: str = "") -> list[dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        terms = query.casefold().split()
        for entry in self.entries():
            if terms and not all(
                term in (entry.doc_id + " " + entry.title).casefold() for term in terms
            ):
                continue
            doc = result.setdefault(
                entry.doc_id,
                {
                    "doc_id": entry.doc_id,
                    "title": entry.title,
                    "page_count": entry.page_count,
                    "renditions": [],
                    "library_id": self.id,
                },
            )
            doc["renditions"].append({"version": entry.version, "rendition": entry.rendition})
        return sorted(result.values(), key=lambda doc: doc["title"].casefold())

    def units(self, entry: Rendition) -> list[dict[str, Any]]:
        if entry.path in self._units_cache:
            return self._units_cache[entry.path]
        name = "chunks.jsonl" if entry.rendition == "basic" else "units.jsonl"
        path = self.safe_path(entry, name)
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("Bundle unit file is too large")
        units = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict):
                continue
            if entry.rendition == "basic":
                unit = {
                    "id": raw["chunk_id"],
                    "kind": raw.get("kind", "text"),
                    "text": raw.get("text", ""),
                    "page": raw.get("page_range", [1])[0],
                    "bbox": raw.get("bbox"),
                    "assets": raw.get("asset_ids", []),
                }
            else:
                text = raw.get("text") or {}
                if not isinstance(text, dict):
                    raise ValueError("Marker unit text must be an object")
                unit = {
                    "id": raw["unit_id"],
                    "kind": raw.get("unit_type", "paragraph"),
                    "text": text.get("body") or text.get("caption") or text.get("ocr") or "",
                    "caption": text.get("caption") or "",
                    "page": raw.get("page", 1),
                    "bbox": raw.get("bbox"),
                    "assets": (raw.get("assets") or {}).get("images", []),
                }
            if not isinstance(unit["id"], str) or not _PART.fullmatch(unit["id"]):
                raise ValueError("Invalid unit id")
            if not isinstance(unit["text"], str) or not isinstance(unit["page"], int):
                raise ValueError("Invalid unit text or page")
            units.append(unit)
        self._units_cache[entry.path] = units
        return units

    def evidence(self, entry: Rendition, unit: dict[str, Any]) -> dict[str, Any]:
        return {
            "library_id": self.id,
            "doc_id": entry.doc_id,
            "title": entry.title,
            "version": entry.version,
            "rendition": entry.rendition,
            "page": unit["page"],
            "bbox": unit.get("bbox"),
            "locator": entry.locator("unit", unit["id"]),
            "kind": unit["kind"],
            "asset_ids": unit.get("assets", []),
        }

    def resolve(self, locator: str) -> tuple[Rendition, str, str]:
        match = _LOCATOR.fullmatch(locator)
        if not match:
            raise ValueError(
                "Invalid document locator; use a locator returned by doc_search or doc_library"
            )
        doc, version, rendition, kind, ident = match.groups()
        self.entries()
        assert self._entries is not None
        entry = self._entries.get((doc, version, rendition))
        if entry is None:
            raise ValueError("Document/version/rendition is unavailable in this library or scope")
        return entry, kind, ident

    def search(self, query: str, doc: str = "", top_k: int = 8, kind: str = "") -> dict[str, Any]:
        terms = [term for term in re.findall(r"[\w]+", query.casefold()) if len(term) > 1]
        if not terms:
            return {
                "hits": [],
                "warnings": ["Use a title, distinctive term, phrase, or figure caption."],
            }
        candidates: list[dict[str, Any]] = []
        entries = [entry for entry in self.entries() if not doc or entry.doc_id == doc]
        basic = {
            (entry.doc_id, entry.version): entry for entry in entries if entry.rendition == "basic"
        }
        indexed = False
        indexed_keys: set[tuple[str, str]] = set()
        warnings = []
        db = self.root / "catalog.sqlite"
        if db.is_file() and db.resolve().is_relative_to(self.root) and not kind:
            try:
                with contextlib.closing(
                    sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True)
                ) as connection:
                    connection.row_factory = sqlite3.Row
                    db_modified = db.stat().st_mtime_ns
                    indexed_keys = {
                        (row["doc_id"], row["version"])
                        for row in connection.execute("SELECT doc_id,version FROM docs")
                        if (row["doc_id"], row["version"]) in basic
                        and (basic[(row["doc_id"], row["version"])].path / "manifest.json").stat().st_mtime_ns <= db_modified
                    }
                    match = " OR ".join('"' + term.replace('"', '""') + '"*' for term in terms)
                    sql = "SELECT u.*,bm25(units_fts) AS rank FROM units_fts JOIN units u ON u.unit_key=units_fts.unit_key WHERE units_fts MATCH ?"
                    params: list[Any] = [match]
                    scope_ids = [doc] if doc else list(self.scope)
                    if scope_ids:
                        sql += " AND u.doc_id IN (" + ",".join("?" for _ in scope_ids) + ")"
                        params.extend(scope_ids)
                    sql += " ORDER BY rank LIMIT ?"
                    params.append(top_k * 3)
                    for row in connection.execute(sql, params):
                        entry = basic.get((row["doc_id"], row["version"]))
                        if entry and (row["doc_id"], row["version"]) in indexed_keys:
                            unit = {
                                "id": row["chunk_id"],
                                "kind": row["kind"],
                                "page": row["page_start"],
                                "bbox": json.loads(row["bbox_json"]) if row["bbox_json"] else None,
                            }
                            candidates.append(
                                {
                                    **self.evidence(entry, unit),
                                    "snippet": row["text"][:400],
                                    "score": sum(term in row["text"].casefold() for term in terms)
                                    / len(terms),
                                }
                            )
                    # unicode61 does not segment Chinese phrases into words.
                    # Substring scanning remains available for CJK queries.
                    indexed = not any(re.search(r"[\u3400-\u9fff]", term) for term in terms)
            except (sqlite3.Error, ValueError, KeyError) as exc:
                warnings.append(
                    f"Full-text index unavailable; lexical fallback: {type(exc).__name__}"
                )
        for entry in entries:
            if indexed and entry.rendition == "basic" and (entry.doc_id, entry.version) in indexed_keys:
                continue
            try:
                units = self.units(entry)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                warnings.append(
                    f"Skipped unreadable {entry.doc_id}/{entry.rendition}: {type(exc).__name__}"
                )
                continue
            for unit in units:
                if kind and unit["kind"] != kind:
                    continue
                text = unit["text"]
                score = sum(term in text.casefold() for term in terms) / len(terms)
                if score:
                    candidates.append(
                        {**self.evidence(entry, unit), "snippet": text[:400], "score": score}
                    )
        candidates = list({hit["locator"]: hit for hit in candidates}.values())
        candidates.sort(
            key=lambda hit: (-hit["score"], hit["rendition"] == "basic", hit["doc_id"], hit["page"])
        )
        return {
            "hits": candidates[: max(1, min(top_k, 20))],
            "warnings": warnings,
            "strategy": "lexical",
            "vector_search": False,
        }

    def read(self, locator: str, max_chars: int = 6000, neighbors: int = 0) -> dict[str, Any]:
        entry, kind, ident = self.resolve(locator)
        units = self.units(entry)
        if kind == "page":
            selected = [unit for unit in units if unit["page"] == int(ident)]
        elif kind == "unit":
            index = next((i for i, unit in enumerate(units) if unit["id"] == ident), None)
            if index is None:
                raise ValueError("Unit not found in the requested rendition")
            selected = units[max(0, index - neighbors) : index + neighbors + 1]
        else:
            raise ValueError("Use doc_view for asset locators")
        if not selected:
            raise ValueError("No text units at this location")
        target = next((unit for unit in selected if unit["id"] == ident), selected[0])
        source = self.evidence(entry, target)
        if kind == "page":
            source = {**source, "locator": locator, "page": int(ident)}
        text = "\n\n".join(unit["text"] for unit in selected)
        if kind == "unit" and neighbors:
            position = selected.index(target)
            parts = ["[Target]\n" + target["text"]]
            if position:
                parts.append(
                    "[Preceding context]\n"
                    + "\n\n".join(unit["text"] for unit in selected[:position])
                )
            if position + 1 < len(selected):
                parts.append(
                    "[Following context]\n"
                    + "\n\n".join(unit["text"] for unit in selected[position + 1 :])
                )
            # The requested source must survive even when neighboring text is
            # longer than the return budget.
            text = "\n\n".join(parts)
        return {
            "source": source,
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
            "evidence": [self.evidence(entry, unit) for unit in selected[:50]],
            "warning": "basic text is line-level; use neighbors or the page image for context"
            if entry.rendition == "basic"
            else "",
        }

    def image(self, locator: str) -> tuple[Path, str, dict[str, Any]]:
        entry, kind, ident = self.resolve(locator)
        asset_id = ident
        evidence: dict[str, Any] = {
            "library_id": self.id,
            "doc_id": entry.doc_id,
            "title": entry.title,
            "version": entry.version,
            "rendition": entry.rendition,
            "locator": locator,
        }
        if kind == "page":
            pages = read_json(self.safe_path(entry, "pages.json")).get("pages", [])
            page = next((page for page in pages if page["page"] == int(ident)), None)
            if page is None:
                raise ValueError("Page not found")
            asset_id = page["render_asset"]
            evidence["page"] = int(ident)
        elif kind == "unit":
            unit = next((unit for unit in self.units(entry) if unit["id"] == ident), None)
            if unit is None:
                raise ValueError("Unit not found")
            evidence.update(self.evidence(entry, unit))
            if not unit.get("assets"):
                return self.image(entry.locator("page", unit["page"]))
            asset_id = unit["assets"][0]
        else:
            related = next(
                (unit for unit in self.units(entry) if ident in unit.get("assets", [])), None
            )
            if related:
                evidence.update(self.evidence(entry, related))
            else:
                pages = read_json(self.safe_path(entry, "pages.json")).get("pages", [])
                evidence["page"] = next(
                    (page["page"] for page in pages if page.get("render_asset") == ident), None
                )
        assets = read_json(self.safe_path(entry, "assets/index.json")).get("assets", {})
        asset = assets.get(asset_id)
        if not isinstance(asset, dict):
            raise ValueError("Image asset not found")
        mime = asset.get("mime", "image/png")
        if not isinstance(mime, str) or mime not in {
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
        }:
            raise ValueError("Asset is not a supported image")
        path = self.safe_path(entry, asset["path"])
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds 5 MiB; choose a figure crop")
        return path, mime, evidence
