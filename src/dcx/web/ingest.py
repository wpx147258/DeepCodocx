"""HTTP adapter for system-side document ingestion.

This is deliberately not an MCP tool: the agent can read documents but cannot
start a multi-minute upload/builder job.
"""

from __future__ import annotations

import asyncio
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
        "status": job.get("status"),
        "current": job.get("current"),
        "files": files,
        "warnings": job.get("warnings", []),
        "error": job.get("error", ""),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
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
            mode = str(body.get("mode") or "basic")
            if mode not in {"basic", "marker", "both"}:
                raise ValueError("mode must be basic, marker or both")
            entry = library_for(str(body.get("library_id") or ""))
            root = Path(entry["root"])
            job = await asyncio.to_thread(
                service().create_job,
                entry["id"],
                root,
                mode,
                force=bool(body.get("force", False)),
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
                raise ValueError("Upload exceeds the configured size limit")
            data = await request.body()
            job = await asyncio.to_thread(service().add_file, job_id, name, data)
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
            if data.get("status") in {"succeeded", "partial"}:
                library = host.bundle.document_library
                if library is not None:
                    library.reload()
            return JSONResponse(_public(data))
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def jobs(request: Request) -> Response:
        try:
            items = await asyncio.to_thread(service().list)
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
        return JSONResponse(service().environment())

    return [
        Route("/api/ingest/environment", environment),
        Route("/api/ingest/jobs", jobs),
        Route("/api/ingest/jobs", create, methods=["POST"]),
        Route("/api/ingest/jobs/{job_id}", job),
        Route("/api/ingest/jobs/{job_id}/files", upload, methods=["POST"]),
        Route("/api/ingest/jobs/{job_id}/start", start, methods=["POST"]),
        Route("/api/ingest/jobs/{job_id}/cancel", cancel, methods=["POST"]),
    ]
