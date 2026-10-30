"""Incremental multimodal indexing and scoped read-only retrieval."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..library import DocumentLibrary
from .client import InferenceClient, image_data
from .samples import Sample, digest, documents, extract, fingerprint
from .settings import CaptionConfig, IndexConfig
from .store import REPRESENTATIONS, VectorStore


class SemanticEngine:
    def __init__(
        self,
        library: DocumentLibrary,
        data_dir: Path,
        index: IndexConfig,
        caption: CaptionConfig,
        client: InferenceClient | None = None,
    ):
        self.library, self.index, self.caption = library, index, caption
        self.inventory_errors: list[dict[str, str]] = []
        self.store = VectorStore(data_dir / "semantic" / library.id / "index.sqlite")
        selected = self.store.preference("caption_model")
        if selected:
            self.caption = replace(caption, model=selected)
        self.client = client or InferenceClient(index, self.caption)
        if client is not None:
            self.client.caption = self.caption

    @property
    def generate(self) -> bool:
        saved = self.store.preference("generate_captions")
        return self.caption.enabled if saved is None else bool(saved)

    @property
    def profile(self) -> str:
        selected = self.store.preference("caption_model")
        if selected and selected != self.caption.model:
            self.caption = replace(self.caption, model=selected)
            self.client.caption = self.caption
        return digest(
            {
                "index": self.index.profile,
                "caption": self.caption.profile if self.generate else None,
            }
        )

    def inventory(self) -> tuple[dict[str, Any], dict[str, str]]:
        self.library.reload()
        groups = documents(self.library)
        live = {}
        invalid = []
        self.inventory_errors = []
        with self.store.connect() as conn:
            for doc_id, entries in groups.items():
                try:
                    live[doc_id] = fingerprint(
                        self.library, entries, lambda path: self.store.hash_file(conn, path)
                    )
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    self.inventory_errors.append({"doc_id": doc_id, "error": str(exc)})
                    invalid.append(doc_id)
        for doc_id in invalid:
            groups.pop(doc_id, None)
        return groups, live

    def status(self) -> dict[str, Any]:
        groups, live = self.inventory()
        profile = self.profile
        with self.store.connect() as conn:
            indexed = {r["doc_id"]: dict(r) for r in conn.execute("SELECT * FROM documents")}
            valid = {
                doc
                for doc in live
                if doc in indexed
                and indexed[doc]["fingerprint"] == live[doc]
                and indexed[doc]["profile"] == profile
            }
            complete = {doc for doc in valid if indexed[doc]["complete"]}
            counts = {name: 0 for name in REPRESENTATIONS}
            for row in conn.execute(
                "SELECT doc_id,profile,fingerprint,representation,COUNT(*) AS n FROM vectors GROUP BY doc_id,profile,fingerprint,representation"
            ):
                if (
                    row["doc_id"] in valid
                    and row["profile"] == profile
                    and row["fingerprint"] == live[row["doc_id"]]
                ):
                    counts[row["representation"]] += row["n"]
        return {
            "library_id": self.library.id,
            "model": self.index.model,
            "joint_model": self.index.joint_model,
            "joint_dimensions": self.index.joint_dimensions,
            "joint_protocol": self.index.joint_protocol,
            "joint_key_env": self.index.joint_key_env,
            "dimensions": self.index.dimensions,
            "profile": profile,
            "fusion": "native",
            "joint_ready": self.client.joint_ready(),
            "generate_captions": self.generate,
            "total_documents": len(groups) + len(self.inventory_errors),
            "indexed_documents": len(complete),
            "partial_documents": len(valid - complete),
            "pending_documents": len(groups) - len(complete) + len(self.inventory_errors),
            "source_errors": self.inventory_errors,
            "counts": counts,
            "stale_documents": len(set(indexed) - valid),
            "auto_sync": self.store.preference("auto_sync")
            if self.store.preference("auto_sync") is not None
            else self.index.auto_sync,
            "documents": [
                {
                    "doc_id": doc,
                    "title": entries[0].title,
                    "status": "ready"
                    if doc in complete
                    else "partial"
                    if doc in valid
                    else "pending",
                }
                for doc, entries in groups.items()
            ],
        }

    def _records(
        self, samples: list[Sample], force: bool, check: Callable[[], None]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        specs = []
        joint = []
        errors: list[str] = []
        for sample in samples:
            check()
            payload = sample.payload()
            payload["sample_id"] = sample.key
            payload["library_id"] = self.library.id
            if sample.family == "text_block":
                specs.append(
                    (
                        sample,
                        "text_block",
                        {
                            "text": sample.title
                            + "\n"
                            + " / ".join(sample.heading_path)
                            + "\n"
                            + sample.text
                        },
                        payload,
                    )
                )
                continue
            image = image_data(Path(sample.image))
            specs.append((sample, "image", {"image": image}, payload))
            captions = []
            if sample.caption.strip():
                captions.append(
                    ("original", sample.caption, {"source": "original", "text": sample.caption})
                )
            if self.generate:
                key = digest([self.caption.profile, image, sample.caption, sample.context])
                try:
                    generated = None if force else self.store.caption(key)
                    if generated is None:
                        description = self.client.describe(image, sample.caption, sample.context)
                        generated = {
                            "source": "generated",
                            "text": description,
                            "model": self.caption.model,
                            "revision": self.caption.revision,
                            "input_hash": key,
                            "generated_at": time.time(),
                            "original_caption": sample.caption,
                            "context_locators": sample.context_sources,
                        }
                        self.store.caption(key, generated)
                    captions.append(("generated", generated["text"], generated))
                except Exception as exc:
                    check()
                    errors.append(f"Caption {sample.locator}: {exc}")
            for origin, caption, provenance in captions:
                expanded = {**payload, "caption_text": caption, "caption_provenance": provenance}
                specs.append((sample, "caption_" + origin, {"text": caption}, expanded))
                joint.append((sample, "image_caption_" + origin, image, caption, expanded))
        vectors = {}
        missing = []
        for sample, representation, value, _payload in specs:
            key = digest([self.index.embedding_profile, value])
            identity = (sample.key, representation)
            vector = None if force else self.store.cache(key)
            if vector is None:
                missing.append((identity, key, value))
            else:
                vectors[identity] = vector
        for start in range(0, len(missing), self.index.batch_size):
            check()
            batch = missing[start : start + self.index.batch_size]
            encoded = self.client.embed([value for _, _, value in batch])
            for (identity, key, _), vector in zip(batch, encoded, strict=True):
                self.store.cache(key, vector)
                vectors[identity] = vector
        records = []
        for sample, representation, _, payload in specs:
            # Absolute local paths are indexing inputs, not source text sent back to tools.
            payload = {k: v for k, v in payload.items() if k not in {"image", "context"}}
            records.append(
                {
                    "id": digest([sample.key, representation]),
                    "representation": representation,
                    "space": "joint" if representation.startswith("image_caption_") else "gitee",
                    "vector": vectors[(sample.key, representation)],
                    "payload": payload,
                }
            )
        for sample, representation, image, text, payload in joint:
            check()
            key = digest([self.index.joint_profile, "joint", image, text])
            try:
                vector = None if force else self.store.cache(key)
                origin = representation.removeprefix("image_caption_")
                if vector is None:
                    vector = self.client.combined(
                        image,
                        text,
                        vectors[(sample.key, "image")],
                        vectors[(sample.key, "caption_" + origin)],
                    )
                    self.store.cache(key, vector)
            except Exception as exc:
                check()
                errors.append(f"Native joint {sample.locator}: {exc}")
                continue
            payload = {k: v for k, v in payload.items() if k not in {"image", "context"}}
            payload["fusion"] = {
                "method": "native",
                "model": self.index.joint_model,
                "protocol": self.index.joint_protocol,
                "space": "joint",
            }
            records.append(
                {
                    "id": digest([sample.key, representation]),
                    "representation": representation,
                    "space": "joint" if representation.startswith("image_caption_") else "gitee",
                    "vector": vector,
                    "payload": payload,
                }
            )
        return records, errors

    def sync(
        self,
        *,
        force: bool = False,
        check: Callable[[], None] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        check = check or (lambda: None)
        progress = progress or (lambda *_: None)
        if not self.index.enabled:
            raise ValueError("Semantic indexing is disabled in configuration")
        if not self.client.ready():
            raise ValueError(f"Set {self.index.api_key_env} in the server environment")
        if self.generate and not self.client.ready(True):
            raise ValueError(f"Set {self.caption.api_key_env} for generated captions")
        groups, live = self.inventory()
        profile = self.profile
        self.store.prune(set(groups))
        with self.store.connect() as conn:
            saved = {row["doc_id"]: dict(row) for row in conn.execute("SELECT * FROM documents")}
        pending = [
            doc
            for doc in groups
            if force
            or doc not in saved
            or saved[doc]["profile"] != profile
            or saved[doc]["fingerprint"] != live[doc]
            or not saved[doc]["complete"]
        ]
        errors: list[dict[str, str]] = list(self.inventory_errors)
        completed = 0
        for position, doc_id in enumerate(pending):
            check()
            progress(position, len(pending), groups[doc_id][0].title)
            try:
                samples = extract(self.library, groups[doc_id], self.index.block_chars)
                records, issues = self._records(samples, force, check)
                check()
                # Refuse a document that changed while requests were in flight.
                with self.store.connect() as conn:
                    actual = fingerprint(
                        self.library, groups[doc_id], lambda path: self.store.hash_file(conn, path)
                    )
                if actual != live[doc_id]:
                    raise ValueError("Bundle changed during indexing; synchronize again")
                self.store.publish(doc_id, actual, profile, records, complete=not issues)
                completed += 1
                errors.extend({"doc_id": doc_id, "error": issue} for issue in issues)
            except Exception as exc:
                check()  # Cancellation escapes; ordinary document failures stay retryable.
                errors.append({"doc_id": doc_id, "error": str(exc)})
            progress(position + 1, len(pending), groups[doc_id][0].title)
        return {
            "completed_documents": completed,
            "unchanged_documents": len(groups) - len(pending),
            "errors": errors,
        }

    def search(
        self,
        text: str = "",
        *,
        image: Path | None = None,
        representations: list[str] | None = None,
        doc: str = "",
        top_k: int = 8,
    ) -> dict[str, Any]:
        if not self.index.enabled:
            raise ValueError("Semantic indexing is disabled")
        if not text.strip() and image is None:
            raise ValueError("Provide query text or an image")
        if len(text) > 12000 or not 1 <= top_k <= 20:
            raise ValueError("Query too long or top_k out of range")
        representations = representations or []
        if any(name not in REPRESENTATIONS for name in representations):
            raise ValueError("Unknown vector representation")
        groups, live = self.inventory()
        if doc:
            live = {key: value for key, value in live.items() if key == doc}
        profile = self.profile
        with self.store.connect() as conn:
            ready = {
                row["doc_id"]
                for row in conn.execute("SELECT doc_id,fingerprint,profile FROM documents")
                if row["doc_id"] in live
                and row["fingerprint"] == live[row["doc_id"]]
                and row["profile"] == profile
            }
        live = {key: value for key, value in live.items() if key in ready}
        if not live:
            return {
                "hits": [],
                "strategy": "semantic",
                "vector_search": True,
                "warnings": [
                    "No current semantic index for this library/scope. Use keyword search or ask the user to synchronize the index."
                ],
            }
        gitee_reps = [
            r
            for r in (representations or list(REPRESENTATIONS))
            if not r.startswith("image_caption_")
        ]
        joint_reps = [
            r for r in (representations or list(REPRESENTATIONS)) if r.startswith("image_caption_")
        ]
        rankings = []
        warnings = []
        data = image_data(image) if image else ""
        if gitee_reps:
            inputs = ([{"text": text}] if text.strip() else []) + (
                [{"image": data}] if data else []
            )
            try:
                for vector in self.client.embed(inputs):
                    rankings.append(
                        self.store.search(vector, profile, live, gitee_reps, top_k, space="gitee")
                    )
            except Exception as exc:
                warnings.append(str(exc))
        if joint_reps:
            try:
                vector = self.client.native_query(text, data)
                rankings.append(
                    self.store.search(vector, profile, live, joint_reps, top_k, space="joint")
                )
            except Exception as exc:
                warnings.append(str(exc))
        merged = {}
        for ranking in rankings:
            for rank, hit in enumerate(ranking):
                key = hit.get("sample_id") or hit["locator"]
                if key not in merged:
                    merged[key] = {
                        **hit,
                        "score": 0.0,
                        "similarities": [],
                        "matched_representations": [],
                    }
                item = merged[key]
                item["score"] += 1 / (60 + rank + 1)
                item["similarities"].append(
                    {
                        "space": "joint"
                        if hit["matched_representations"][0].startswith("image_caption_")
                        else "gitee",
                        "cosine": hit["score"],
                    }
                )
                item["matched_representations"] = list(
                    dict.fromkeys(
                        [*item["matched_representations"], *hit["matched_representations"]]
                    )
                )
        hits = sorted(merged.values(), key=lambda item: -item["score"])[:top_k]
        for hit in hits:
            hit["snippet"] = (
                hit.get("caption_text") or hit.get("text") or hit.get("caption") or ""
            )[:500]
            hit.pop("text", None)
            # Keep sources and provenance; do not return unbounded full block text.
            if "caption_provenance" in hit:
                hit["caption_provenance"] = {
                    k: v for k, v in hit["caption_provenance"].items() if k != "text"
                }
            hit.pop("caption_text", None)
        return {
            "hits": hits,
            "strategy": "semantic",
            "vector_search": True,
            "model": self.index.model,
            "fusion": "native",
            "score_type": "reciprocal_rank_fusion",
            "warnings": warnings,
        }
