"""HTTP adapter for system-side document ingestion.

This is deliberately not an MCP tool: the agent can read documents but cannot
start a multi-minute upload/builder job.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from .server import WebHost


def _public(job: dict[str, Any]) -> dict[str, Any]:
    files = []
    for item in job.get("files", []):
        files.append(
            {
                "name": item.get("name"),
                "doc_id": item.get("doc_id"),
                "size": item.get("size"),
                "status": item.get("status"),
                "modes": item.get("modes", {}),
                "error": item.get("error", ""),
            }
        )
    return {
        "id": job.get("id"),
        "library_id": job.get("library_id"),
        "mode": job.get("mode"),
        "force": job.get("force", False),
        "status": job.get("status"),
        "current": job.get("current"),
        "files": files,
        "warnings": job.get("warnings", []),
        "error": job.get("error", ""),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "result": job.get("result", {}),
    }


def ingest_routes(host: WebHost) -> list[Route]:
    def service():
        return host.ingest

    def library_for(library_id: str):
        if host.bundle.libraries is None:
            raise ValueError("Document libraries are not configured")
        return host.bundle.libraries.get(library_id or host.bundle.libraries.selected_id())

    async def create(request: Request) -> Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            mode = body.get("mode", "basic")
            if not isinstance(mode, str):
                raise ValueError("mode must be basic, marker or both")
            if mode not in {"basic", "marker", "both"}:
                raise ValueError("mode must be basic, marker or both")
            library_id = body.get("library_id", "")
            if not isinstance(library_id, str):
                raise ValueError("library_id must be a string")
            force = body.get("force", False)
            if not isinstance(force, bool):
                raise ValueError("force must be a boolean")
            entry = library_for(library_id)
            root = Path(entry["root"])
            job = await asyncio.to_thread(
                service().create_job,
                entry["id"],
                root,
                mode,
                force=force,
            )
            return JSONResponse(_public(job), status_code=202)
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def upload(request: Request) -> Response:
        job_id = request.path_params["job_id"]
        try:
            name = request.query_params.get("name", "")
            length = request.headers.get("content-length")
            limit = service().settings.max_upload_mb * 1024 * 1024
            if length and int(length) > limit:
                return JSONResponse(
                    {"error": "Upload exceeds the configured size limit"}, status_code=413
                )
            current = await asyncio.to_thread(service().get, job_id)
            if current["status"] != "uploading":
                raise ValueError("This job is no longer accepting uploads")
            # Bound memory even without Content-Length, and remove partial data
            # on cancellation, disconnection, validation failure or oversize.
            with tempfile.TemporaryDirectory(prefix="incoming-", dir=service().root) as temporary:
                path = Path(temporary) / "upload.pdf"
                size = 0
                with path.open("wb") as handle:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > limit:
                            return JSONResponse(
                                {"error": "Upload exceeds the configured size limit"},
                                status_code=413,
                            )
                        await asyncio.to_thread(handle.write, chunk)
                job = await asyncio.to_thread(service().add_file, job_id, name, path)
            return JSONResponse(_public(job))
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def start(request: Request) -> Response:
        job_id = request.path_params["job_id"]
        try:
            job = await asyncio.to_thread(service().start, job_id)
            return JSONResponse(_public(job), status_code=202)
        except (ValueError, OSError, RuntimeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def job(request: Request) -> Response:
        job_id = request.path_params["job_id"]
        try:
            data = await asyncio.to_thread(service().get, job_id)
            return JSONResponse(_public(data))
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def jobs(request: Request) -> Response:
        try:
            items = await asyncio.to_thread(service().list)
            library_id = request.query_params.get("library_id")
            if library_id:
                items = [item for item in items if item.get("library_id") == library_id]
            return JSONResponse({"jobs": [_public(item) for item in items[:50]]})
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def cancel(request: Request) -> Response:
        job_id = request.path_params["job_id"]
        try:
            return JSONResponse(_public(await asyncio.to_thread(service().cancel, job_id)))
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def environment(request: Request) -> Response:
        return JSONResponse(await asyncio.to_thread(service().environment))

    return [
        Route("/api/ingest/environment", environment),
        Route("/api/ingest/jobs", jobs),
        Route("/api/ingest/jobs", create, methods=["POST"]),
        Route("/api/ingest/jobs/{job_id}", job),
        Route("/api/ingest/jobs/{job_id}/files", upload, methods=["POST"]),
        Route("/api/ingest/jobs/{job_id}/start", start, methods=["POST"]),
        Route("/api/ingest/jobs/{job_id}/cancel", cancel, methods=["POST"]),
    ]
