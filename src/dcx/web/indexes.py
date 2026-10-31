"""Authenticated HTTP controls for library indexes and query previews."""

from __future__ import annotations

import asyncio
import base64
import io
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from .server import WebHost


def index_routes(host: WebHost) -> list[Route]:
    def library(ident: str):
        if not host.bundle.libraries:
            raise ValueError("Document libraries are not configured")
        return host.bundle.libraries.open(ident or host.bundle.libraries.selected_id())

    async def status(request: Request):
        try:
            selected = library(request.query_params.get("library_id", ""))
            result = await asyncio.to_thread(host.indexes.status, selected)
            return JSONResponse(result)
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def mutate(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            if not isinstance(body.get("library_id", ""), str):
                raise ValueError("library_id must be a string")
            selected = library(body.get("library_id", ""))
            if request.url.path.endswith("/cancel"):
                ident = body.get("job_id")
                if not isinstance(ident, str):
                    raise ValueError("job_id is required")
                result = await asyncio.to_thread(host.indexes.cancel, selected, ident)
            else:
                for key in ("force", "generate_captions"):
                    if key in body and not isinstance(body[key], bool):
                        raise ValueError(f"{key} must be a boolean")
                result = await asyncio.to_thread(
                    host.indexes.start,
                    selected,
                    generate=body.get("generate_captions"),
                    force=body.get("force", False),
                    caption_model=body.get("caption_model"),
                    documents=body.get("documents"),
                )
            return JSONResponse(result, status_code=202)
        except (ValueError, OSError, RuntimeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def search(request: Request):
        try:
            if int(request.headers.get("content-length", "0")) > 8 * 1024 * 1024:
                raise ValueError("Query request too large")
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("Query request too large")
            import json

            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            for key in ("library_id", "text", "image", "doc"):
                if not isinstance(body.get(key, ""), str):
                    raise ValueError(f"{key} must be a string")
            selected = library(body.get("library_id", ""))
            # Current session scope remains authoritative for its selected library.
            current = host.bundle.document_library
            if current is not None and current.id == selected.id:
                selected = current
            representations = body.get("representations", [])
            if not isinstance(representations, list) or not all(
                isinstance(x, str) for x in representations
            ):
                raise ValueError("representations must be an array of strings")
            top_k = body.get("top_k", 8)
            if type(top_k) is not int:
                raise ValueError("top_k must be an integer")
            with tempfile.TemporaryDirectory(prefix="dcx-query-") as temporary:
                image = None
                if body.get("image"):
                    data = body["image"]
                    if not data.startswith("data:image/"):
                        raise ValueError("Provide an image data URL")
                    content = base64.b64decode(data.split(",", 1)[1], validate=True)
                    if len(content) > 5 * 1024 * 1024:
                        raise ValueError("Query image exceeds 5 MiB")
                    with Image.open(io.BytesIO(content)) as decoded:
                        decoded.verify()
                    image = Path(temporary) / "query-image"
                    image.write_bytes(content)
                result = await asyncio.to_thread(
                    host.indexes.engine(selected).search,
                    body.get("text", ""),
                    image=image,
                    representations=representations,
                    doc=body.get("doc", ""),
                    top_k=top_k,
                )
            return JSONResponse(result)
        except (ValueError, OSError, RuntimeError, KeyError, IndexError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    return [
        Route("/api/index/status", status),
        Route("/api/index/sync", mutate, methods=["POST"]),
        Route("/api/index/cancel", mutate, methods=["POST"]),
        Route("/api/index/search", search, methods=["POST"]),
    ]
