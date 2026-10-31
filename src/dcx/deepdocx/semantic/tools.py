"""Read-only MCP schemas and query-image resolution; no build control tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...session.attachments import AttachmentStore
from .engine import SemanticEngine
from .store import REPRESENTATIONS

SCHEMAS = [
    {
        "name": "doc_index_status",
        "description": "Inspect semantic index coverage, stale documents and multimodal retrieval availability. Read-only 索引 状态 向量 可用性",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "doc_semantic_search",
        "description": "Search documents semantically by paraphrased text, image or image+text. Use image_ref for a user attachment or library image; target=auto/text/image. Returns a few source cards and indexed coverage. Read originals with doc_read/doc_view. 文档 论文 语义检索 以图搜图 图文",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 12000},
                "image_ref": {
                    "type": "string",
                    "description": "Use an attachment:... reference from host attachment context, or an exact doc:... image locator from this library. Do not invent a reference or send base64.",
                },
                "target": {
                    "type": "string",
                    "enum": ["auto", "text", "image"],
                    "default": "auto",
                    "description": "auto: text/caption evidence for text queries, image/joint evidence when an image is supplied. text: source text blocks. image: figures/pages via images, captions and native fusion.",
                },
                "image_locator": {"type": "string"},
                "image_path": {
                    "type": "string",
                    "description": "Local image under the workspace, selected library, or session data; remote URLs are not accepted.",
                },
                "representations": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(REPRESENTATIONS)},
                    "maxItems": 6,
                    "description": "Optional advanced filter; normally use target instead. Cannot combine with an explicit text/image target.",
                },
                "doc": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "additionalProperties": False,
        },
    },
]


def call(
    engine: SemanticEngine,
    name: str,
    arguments: dict[str, Any],
    allowed_roots: tuple[Path, ...],
    attachments: AttachmentStore | None = None,
) -> dict[str, Any]:
    if name == "doc_index_status":
        status = engine.status()
        result = {key: value for key, value in status.items() if key != "documents"}
        result["source_errors"] = result["source_errors"][:20]
        return result
    image = None
    if sum(bool(arguments.get(key)) for key in ("image_ref", "image_locator", "image_path")) > 1:
        raise ValueError("Use only one of image_ref, image_locator or image_path")
    reference = arguments.get("image_ref", "")
    if reference:
        if reference.startswith("attachment:"):
            if attachments is None:
                raise ValueError("No session attachments are connected")
            image = attachments.resolve(reference)
        elif reference.startswith("doc:"):
            image, _, _ = engine.library.image(reference)
        else:
            raise ValueError("image_ref must be a host attachment reference or library doc locator")
    elif arguments.get("image_locator"):
        image, _, _ = engine.library.image(arguments["image_locator"])
    elif arguments.get("image_path"):
        image = Path(arguments["image_path"]).expanduser().resolve(strict=True)
        if not any(image.is_relative_to(root.resolve()) for root in allowed_roots):
            raise ValueError("Query image is outside the allowed workspace/library/session roots")
    target = arguments.get("target", "auto")
    if target not in {"auto", "text", "image"}:
        raise ValueError("target must be auto, text or image")
    representations = arguments.get("representations") or []
    if representations and target != "auto":
        raise ValueError("Use target or advanced representations, not both")
    if not representations:
        if target == "text":
            representations = ["text_block"]
        elif target == "image":
            representations = [value for value in REPRESENTATIONS if value != "text_block"]
        elif image is not None:
            representations = ["image", "image_caption_original", "image_caption_generated"]
        else:
            representations = ["text_block", "caption_original", "caption_generated"]
    result = engine.search(
        arguments.get("query", ""),
        image=image,
        representations=representations,
        doc=arguments.get("doc", ""),
        top_k=arguments.get("top_k", 5),
    )
    result["query_mode"] = (
        "image_text"
        if image is not None and arguments.get("query", "").strip()
        else "image"
        if image is not None
        else "text"
    )
    result["target"] = target
    result["hits"] = [_card(hit) for hit in result["hits"]]
    return result


def _card(hit: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "library_id",
        "doc_id",
        "title",
        "version",
        "rendition",
        "page",
        "bbox",
        "locator",
        "image_kind",
        "score",
        "similarities",
        "matched_representations",
        "snippet",
    )
    card = {key: hit[key] for key in fields if key in hit}
    sources = hit.get("sources", [])
    card["sources"] = sources[:8]
    card["sources_truncated"] = len(sources) > 8
    provenance = hit.get("caption_provenance")
    if provenance:
        card["caption_provenance"] = {
            key: provenance[key]
            for key in ("source", "model", "revision", "generated_at", "input_hash")
            if key in provenance
        }
        card["snippet_source"] = (
            "generated_description"
            if provenance.get("source") == "generated"
            else "original_caption"
        )
    else:
        card["snippet_source"] = (
            "source_text"
            if hit.get("family") == "text_block"
            else "original_caption"
            if hit.get("caption")
            else "image"
        )
    return card
