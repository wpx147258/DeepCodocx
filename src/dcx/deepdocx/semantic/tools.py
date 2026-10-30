"""Read-only MCP schemas and query-image resolution; no build control tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
        "description": "Locate evidence using text-block, image, caption or native image+caption embeddings. Returns source locators, not answers. 语义检索 以图搜图 文本块 图像 图注",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 12000},
                "image_locator": {"type": "string"},
                "image_path": {
                    "type": "string",
                    "description": "Local image under the workspace, selected library, or session data; remote URLs are not accepted.",
                },
                "representations": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(REPRESENTATIONS)},
                    "maxItems": 6,
                },
                "doc": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "additionalProperties": False,
        },
    },
]


def call(
    engine: SemanticEngine, name: str, arguments: dict[str, Any], allowed_roots: tuple[Path, ...]
) -> dict[str, Any]:
    if name == "doc_index_status":
        status = engine.status()
        return {key: value for key, value in status.items() if key != "documents"}
    image = None
    if arguments.get("image_locator") and arguments.get("image_path"):
        raise ValueError("Use image_locator or image_path, not both")
    if arguments.get("image_locator"):
        image, _, _ = engine.library.image(arguments["image_locator"])
    elif arguments.get("image_path"):
        image = Path(arguments["image_path"]).expanduser().resolve(strict=True)
        if not any(image.is_relative_to(root.resolve()) for root in allowed_roots):
            raise ValueError("Query image is outside the allowed workspace/library/session roots")
    return engine.search(
        arguments.get("query", ""),
        image=image,
        representations=arguments.get("representations"),
        doc=arguments.get("doc", ""),
        top_k=arguments.get("top_k", 8),
    )
