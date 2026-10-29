"""DeepDocx HTTP adapter. All session construction goes through assembly."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from ..assembly import build_runtime
from ..deepdocx.profile import SessionProfile
from ..errors import CliAgentError
from ..session.paths import latest_session_id

if TYPE_CHECKING:
    from .server import WebAsker, WebHost


def document_routes(host: WebHost, asker: WebAsker | None) -> list[Route]:
    async def libraries(request: Request) -> Response:
        store = host.bundle.libraries
        if store is None:
            return JSONResponse({"libraries": [], "selected_library_id": ""})
        try:
            registered_id = None
            if request.method == "POST":
                body = await request.json()
                if not isinstance(body, dict) or not isinstance(body.get("root"), str):
                    raise ValueError("A server-side root directory is required")
                entry = await asyncio.to_thread(
                    store.register, body["root"], str(body.get("name", ""))
                )
                registered_id = entry["id"]
            return JSONResponse(
                {
                    "libraries": store.entries(),
                    "selected_library_id": store.selected_id(),
                    "registered_library_id": registered_id,
                }
            )
        except (ValueError, OSError, CliAgentError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def mode(request: Request) -> Response:
        if host.busy:
            return JSONResponse(
                {"error": "Stop the current turn before switching modes"}, status_code=409
            )
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            target = body.get("mode")
            if target not in ("normal", "deepdocx"):
                raise ValueError("Unknown mode")
            if target == "normal":
                previous = latest_session_id(host.bundle.config.paths, mode="normal")
                if host._factory is not None:
                    await host.open_session(previous)
                    return JSONResponse(host.state_payload())
                profile = SessionProfile()
            else:
                if host.bundle.libraries is None:
                    raise ValueError("Document libraries are not configured")
                entry = host.bundle.libraries.get(
                    str(body.get("library_id") or host.bundle.libraries.selected_id())
                )
                documents = body.get("documents", [])
                if not isinstance(documents, list) or not all(
                    isinstance(item, str) for item in documents
                ):
                    raise ValueError("documents must be an array of document IDs")
                profile = SessionProfile("deepdocx", entry["id"], entry["root"], tuple(documents))
            fork = body.get("fork", False)
            if not isinstance(fork, bool):
                raise ValueError("fork must be a boolean")
            options = replace(
                host.bundle.options,
                mode=None,
                library=None,
                session_profile=profile,
                resume_id=host.bundle.session_id if fork else None,
                fork=fork,
                asker=asker,
            )
            bundle = await asyncio.to_thread(build_runtime, options)
            await host.replace_bundle(bundle)
            return JSONResponse(host.state_payload())
        except (ValueError, OSError, CliAgentError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def documents(request: Request) -> Response:
        library = host.bundle.document_library
        if request.query_params.get("library_id") and host.bundle.libraries:
            try:
                library = host.bundle.libraries.open(request.query_params["library_id"])
            except (ValueError, OSError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        if library is None:
            return JSONResponse({"documents": []})
        try:
            items = await asyncio.to_thread(library.documents, request.query_params.get("q", ""))
            return JSONResponse({"documents": items})
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def evidence(request: Request) -> Response:
        library = host.bundle.document_library
        if library is None:
            return JSONResponse({"error": "Select a DeepDocx library first"}, status_code=400)
        locator = request.query_params.get("locator", "")
        try:
            if request.query_params.get("image") == "1":
                path, mime, _ = await asyncio.to_thread(library.image, locator)
                return FileResponse(path, media_type=mime, headers={"Cache-Control": "no-store"})
            entry, kind, ident = library.resolve(locator)
            if kind == "asset":
                _, _, result = await asyncio.to_thread(library.image, locator)
                return JSONResponse({"text": "", "evidence": [result], "image_locator": locator})
            result = await asyncio.to_thread(
                library.read, locator, 16000, 3 if kind == "unit" else 0
            )
            page = int(ident) if kind == "page" else result["source"]["page"]
            result["image_locator"] = (
                locator
                if kind == "unit" and result["source"].get("asset_ids")
                else entry.locator("page", page)
            )
            return JSONResponse(result)
        except (OSError, ValueError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    return [
        Route("/api/libraries", libraries, methods=["GET", "POST"]),
        Route("/api/mode", mode, methods=["POST"]),
        Route("/api/documents", documents),
        Route("/api/evidence", evidence),
    ]
