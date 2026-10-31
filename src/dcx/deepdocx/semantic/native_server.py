"""Optional same-model native multimodal endpoint; deployed outside the dcx host.

Install services/qwen-native/requirements.txt in a separate GPU environment.
Run: QWEN_VL_JOINT_API_KEY=... python -m dcx.deepdocx.semantic.native_server
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

from PIL import Image
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .client import normalize
from .settings import MODEL


def create_app(encoder: Any = None, token: str | None = None) -> Starlette:
    token = token or os.environ.get("QWEN_VL_JOINT_API_KEY")
    if not token:
        raise ValueError("Set QWEN_VL_JOINT_API_KEY before serving native embeddings")
    lock = asyncio.Lock()
    state = {"encoder": encoder}

    @asynccontextmanager
    async def lifespan(app):
        if state["encoder"] is None:
            from importlib import import_module

            cls = import_module("sentence_transformers").SentenceTransformer
            state["encoder"] = await asyncio.to_thread(
                cls, os.environ.get("QWEN_VL_MODEL_PATH", "Qwen/Qwen3-VL-Embedding-8B")
            )
        yield

    async def embeddings(request: Request):
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, "Bearer " + token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 16 * 1024 * 1024:
                    raise ValueError("Request exceeds 16 MiB")
            body = json.loads(raw)
            if not isinstance(body, dict) or body.get("model") != MODEL:
                raise ValueError(f"model must be {MODEL}")
            dimensions = body.get("dimensions", 4096)
            if type(dimensions) is not int or not 64 <= dimensions <= 4096:
                raise ValueError("Invalid dimensions")
            inputs = body.get("input")
            if not isinstance(inputs, list) or not 1 <= len(inputs) <= 8:
                raise ValueError("Use 1–8 input samples")
            normalized = []
            for item in inputs:
                if not isinstance(item, dict) or not item or set(item) - {"text", "image"}:
                    raise ValueError("Each sample must contain text, image, or both")
                sample: dict[str, Any] = {}
                if "text" in item:
                    if not isinstance(item["text"], str) or len(item["text"]) > 12000:
                        raise ValueError("Invalid text sample")
                    sample["text"] = item["text"]
                if "image" in item:
                    if not isinstance(item["image"], str) or not item["image"].startswith(
                        "data:image/"
                    ):
                        raise ValueError(
                            "Images must be inline data URLs; remote URLs and paths are not accepted"
                        )
                    image_bytes = base64.b64decode(item["image"].split(",", 1)[1], validate=True)
                    with Image.open(io.BytesIO(image_bytes)) as image:
                        if image.width * image.height > 40_000_000:
                            raise ValueError("Image exceeds pixel limit")
                        sample["image"] = image.convert("RGB").copy()
                normalized.append(sample)
            async with lock:
                if state["encoder"] is None:
                    raise ValueError("Model is still loading")
                # Both modalities stay in the SAME dictionary passed to the real
                # multimodal encoder. There is no post-hoc vector fusion.
                vectors = await asyncio.to_thread(
                    state["encoder"].encode,
                    normalized,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
            data = []
            for index, vector in enumerate(vectors):
                values = [float(v) for v in vector]
                if len(values) < dimensions:
                    raise ValueError("Encoder output is smaller than requested dimensions")
                data.append(
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": normalize(values[:dimensions], dimensions),
                    }
                )
            return JSONResponse({"object": "list", "model": MODEL, "data": data})
        except (ValueError, OSError, KeyError, IndexError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def health(request: Request):
        return JSONResponse(
            {"model": MODEL, "ready": state["encoder"] is not None, "native_joint": True}
        )

    return Starlette(
        routes=[Route("/v1/embeddings", embeddings, methods=["POST"]), Route("/health", health)],
        lifespan=lifespan,
    )


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)
