"""HTTP/SSE host for the local chat client.

The server is a thin adapter: ``AgentRuntime`` still owns the transcript,
provider, and usage. One turn runs at a time; permission prompts are bridged
to the browser and fail closed when nobody answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
import uuid
import webbrowser
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..assembly import RuntimeBundle
from ..config import resolve_api_key
from ..events import (
    AgentContextEvent,
    AgentError,
    AgentEvent,
    AgentNotice,
    AgentRetry,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnFinished,
    AgentUsage,
)
from ..messages import (
    ImageBlock,
    Message,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from ..permissions.policy import Answer, PermissionRequest
from ..session.paths import SessionPaths, list_session_ids

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
PERMISSION_TIMEOUT_SECONDS = 600.0
HEARTBEAT_SECONDS = 15.0
_END = object()


class WebAsker:
    """Permission asker backed by the browser; denies when nothing answers."""

    def __init__(self) -> None:
        self._host: WebHost | None = None

    def bind(self, host: WebHost) -> None:
        self._host = host

    async def ask(self, request: PermissionRequest) -> Answer:
        if self._host is None:
            return "no"
        return await self._host.request_permission(request)


async def _wait_for_disconnect(request: Request) -> None:
    """Starlette has no cross-version public disconnect event; poll lightly."""
    while True:
        await asyncio.sleep(0.5)
        if await request.is_disconnected():
            return


type BundleFactory = Callable[[str | None], RuntimeBundle]


class WebHost:
    """Owns the runtime, the active turn, and browser permission futures."""

    def __init__(
        self,
        bundle: RuntimeBundle,
        factory: BundleFactory | None = None,
    ) -> None:
        self.bundle = bundle
        self.runtime = bundle.runtime
        self._factory = factory
        self._turn: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[Any] | None = None
        self._permissions: dict[str, asyncio.Future[Answer]] = {}
        self._closed = False

    async def open_session(self, session_id: str | None) -> None:
        """Build a new runtime (fresh or resumed) and swap it in."""
        if self._factory is None:
            raise RuntimeError("session switching is not configured")
        if session_id is not None and session_id == self.bundle.session_id:
            return
        bundle = await asyncio.to_thread(self._factory, session_id)
        await self.replace_bundle(bundle)

    async def replace_bundle(self, bundle: RuntimeBundle) -> None:
        await self.cancel_turn()
        old = self.runtime
        self.bundle = bundle
        self.runtime = bundle.runtime
        await old.close()

    @property
    def busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    async def request_permission(self, request: PermissionRequest) -> Answer:
        queue = self._queue
        if queue is None or self._closed:
            return "no"
        request_id = uuid.uuid4().hex[:12]
        future: asyncio.Future[Answer] = (
            asyncio.get_running_loop().create_future()
        )
        self._permissions[request_id] = future
        await queue.put(
            {
                "type": "permission_request",
                "id": request_id,
                "tool": request.tool_name,
                "reason": request.reason,
                "input": dict(request.tool_input),
                "scope": request.scope,
            }
        )
        try:
            return await asyncio.wait_for(
                future, timeout=PERMISSION_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.info("permission request timed out; denying")
            return "no"
        finally:
            self._permissions.pop(request_id, None)

    def resolve_permission(self, request_id: str, decision: Answer) -> bool:
        future = self._permissions.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    async def cancel_turn(self) -> bool:
        task = self._turn
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return True

    async def close(self) -> None:
        self._closed = True
        await self.cancel_turn()
        await self.runtime.close()

    def state_payload(self) -> dict[str, Any]:
        runtime = self.runtime
        return {
            "version": __version__,
            "session_id": self.bundle.session_id,
            "provider": runtime.provider_name,
            "model": runtime.model,
            "cwd": str(self.bundle.config.paths.cwd),
            "permission_mode": runtime.permission_mode,
            "context_tokens": runtime.context_tokens,
            "context_window": runtime.context_window,
            "usage": _usage(runtime.usage),
            "last_usage": _usage(runtime.last_usage),
            "messages": [_message(message) for message in runtime.messages],
            "models": _model_choices(self.bundle),
            "thinking": runtime.thinking is not None,
            "memory_enabled": self.bundle.config.memory_enabled,
            "resumed": self.bundle.resumed,
            "forked_from": self.bundle.forked_from,
            "degraded": self.bundle.degraded,
        }

    async def _run_turn(self, text: str, queue: asyncio.Queue[Any]) -> None:
        try:
            async for event in self.runtime.run_turn(text):
                await queue.put(_event(event))
        except asyncio.CancelledError:
            await queue.put({"type": "cancelled"})
            await queue.put(
                {
                    "type": "done",
                    "stop_reason": "cancelled",
                    "total": _usage(self.runtime.usage),
                }
            )
            raise
        except Exception as exc:  # noqa: BLE001 - report, do not kill the host
            logger.exception("web turn failed")
            await queue.put(
                {
                    "type": "error",
                    "kind": "fatal",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
            await queue.put(
                {
                    "type": "done",
                    "stop_reason": "error",
                    "total": _usage(self.runtime.usage),
                }
            )
        finally:
            await queue.put(_END)

    async def stream_turn(
        self, text: str, request: Request
    ) -> AsyncIterator[str]:
        if self.busy:
            raise TurnBusy
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._queue = queue
        self._turn = asyncio.create_task(self._run_turn(text, queue))
        disconnect = asyncio.create_task(_wait_for_disconnect(request))
        try:
            yield _sse(
                {
                    "type": "turn_start",
                    "model": self.runtime.model,
                    "provider": self.runtime.provider_name,
                }
            )
            while True:
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {getter, disconnect},
                    timeout=HEARTBEAT_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if getter not in done:
                    getter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await getter
                    if disconnect in done:
                        break
                    yield ": ping\n\n"
                    continue
                item = getter.result()
                if item is _END:
                    break
                yield _sse(item)
        finally:
            disconnect.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await disconnect
            task = self._turn
            if task is not None:
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self._turn = None
            self._queue = None
            for future in self._permissions.values():
                if not future.done():
                    future.set_result("no")
            self._permissions.clear()


class TurnBusy(RuntimeError):
    """Another turn is already streaming."""


# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------


def _usage(usage: Usage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }


def _block(block: Any) -> dict[str, Any] | None:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ReasoningBlock):
        return {"type": "reasoning", "text": block.text}
    if isinstance(block, ImageBlock):
        return {"type": "image", "media_type": block.media_type, "data": block.data}
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": dict(block.input),
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "is_error": block.is_error,
            "content": [
                item
                for item in (_block(inner) for inner in block.content)
                if item is not None
            ],
        }
    return None


def _message(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "role": message.role,
        "created_at": message.created_at,
        "content": [
            item
            for item in (_block(block) for block in message.content)
            if item is not None
        ],
    }


def _event(event: AgentEvent) -> dict[str, Any]:
    if isinstance(event, AgentTextDelta):
        return {
            "type": "reasoning" if event.reasoning else "text",
            "text": event.text,
        }
    if isinstance(event, AgentToolStarted):
        return {
            "type": "tool_started",
            "id": event.tool_use_id,
            "name": event.name,
            "input": dict(event.input),
        }
    if isinstance(event, AgentToolFinished):
        return {
            "type": "tool_finished",
            "id": event.tool_use_id,
            "name": event.name,
            "content": [
                item
                for item in (_block(block) for block in event.content)
                if item is not None
            ],
            "is_error": event.is_error,
        }
    if isinstance(event, AgentUsage):
        return {
            "type": "usage",
            "turn": _usage(event.turn),
            "total": _usage(event.total),
        }
    if isinstance(event, AgentContextEvent):
        return {"type": "context", "kind": event.kind, "detail": event.detail}
    if isinstance(event, AgentNotice):
        return {"type": "notice", "text": event.text}
    if isinstance(event, AgentRetry):
        return {
            "type": "retry",
            "attempt": event.attempt,
            "max_attempts": event.max_attempts,
            "kind": event.kind,
            "reason": event.reason,
            "partial": event.partial,
        }
    if isinstance(event, AgentError):
        return {
            "type": "error",
            "message": event.message,
            "kind": event.kind,
            "status_code": event.status_code,
        }
    if isinstance(event, AgentTurnFinished):
        return {
            "type": "done",
            "stop_reason": event.stop_reason,
            "total": _usage(event.total),
        }
    return {"type": "unknown"}


def _model_choices(bundle: RuntimeBundle) -> list[dict[str, str]]:
    runtime = bundle.runtime
    choices: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name, provider in bundle.config.providers.items():
        model = provider.default_model
        if not model or (name, model) in seen:
            continue
        if (name, model) != (runtime.provider_name, runtime.model):
            try:
                if not resolve_api_key(provider, os.environ):
                    continue
            except Exception:  # noqa: BLE001 - a broken preset is not selectable
                continue
        seen.add((name, model))
        choices.append(
            {
                "provider": name,
                "model": model,
                "label": f"{name} · {model}",
            }
        )
    current = (runtime.provider_name, runtime.model)
    if current not in seen:
        choices.append(
            {
                "provider": current[0],
                "model": current[1],
                "label": f"{current[0]} · {current[1]}",
            }
        )
    return choices


def _sse(payload: Mapping[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def session_summaries(
    app_paths: Any, *, current: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Recent sessions with a title derived from the first user message."""
    entries: list[tuple[float, dict[str, Any]]] = []
    for session_id in list_session_ids(app_paths)[:50]:
        paths = SessionPaths.for_session(app_paths, session_id)
        title = ""
        created_at = ""
        provider = ""
        model = ""
        try:
            with paths.jsonl.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index >= 500:
                        break
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    kind = record.get("type")
                    if kind == "session":
                        created_at = str(record.get("created_at", ""))
                        provider = str(record.get("provider", ""))
                        model = str(record.get("model", ""))
                    elif kind == "clear":
                        title = ""
                    elif kind == "message" and not title:
                        message = record.get("message")
                        if not isinstance(message, dict) or message.get("role") != "user":
                            continue
                        content = message.get("content", [])
                        if not isinstance(content, list):
                            continue
                        text = "".join(
                            str(block.get("text", ""))
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "text"
                        ).strip()
                        if text:
                            title = text[:160]
                            break
        except OSError:
            continue
        if not title:
            continue
        try:
            mtime = paths.jsonl.stat().st_mtime
        except OSError:
            mtime = 0.0
        entries.append(
            (
                mtime,
                {
                    "id": session_id,
                    "title": title,
                    "created_at": created_at,
                    "provider": provider,
                    "model": model,
                    "current": session_id == current,
                },
            )
        )
    entries.sort(key=lambda item: item[0], reverse=True)
    return [summary for _, summary in entries[:limit]]


async def _security_headers(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'",
    )
    return response


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def create_app(
    bundle: RuntimeBundle,
    asker: WebAsker | None = None,
    factory: BundleFactory | None = None,
) -> Starlette:
    host = WebHost(bundle, factory)
    if asker is not None:
        asker.bind(host)

    async def index(request: Request) -> Response:
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    async def favicon(request: Request) -> Response:
        return FileResponse(STATIC_DIR / "favicon.svg")

    async def bootstrap(request: Request) -> Response:
        return JSONResponse(host.state_payload())

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "model": host.runtime.model})

    async def chat(request: Request) -> Response:
        body = await _json_object(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        text = str(body.get("text", "")).strip()
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)
        if host.busy:
            return JSONResponse({"error": "a turn is already running"}, status_code=409)
        return StreamingResponse(
            host.stream_turn(text, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def permission(request: Request) -> Response:
        body = await _json_object(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        request_id = str(body.get("id", ""))
        decision = str(body.get("decision", ""))
        if decision not in ("yes", "no", "always"):
            return JSONResponse({"error": "invalid decision"}, status_code=400)
        if not host.resolve_permission(request_id, decision):  # type: ignore[arg-type]
            return JSONResponse({"error": "unknown permission request"}, status_code=404)
        return JSONResponse({"ok": True})

    async def cancel(request: Request) -> Response:
        return JSONResponse({"ok": await host.cancel_turn()})

    async def clear(request: Request) -> Response:
        if host.busy:
            return JSONResponse({"error": "stop the current turn first"}, status_code=409)
        await host.runtime.clear()
        return JSONResponse(host.state_payload())

    async def sessions(request: Request) -> Response:
        return JSONResponse(
            {
                "sessions": session_summaries(
                    host.bundle.config.paths,
                    current=host.bundle.session_id,
                )
            }
        )

    async def new_session(request: Request) -> Response:
        if host.busy:
            return JSONResponse({"error": "stop the current turn first"}, status_code=409)
        try:
            await host.open_session(None)
        except Exception as exc:  # noqa: BLE001 - surfaced to the client
            return JSONResponse(
                {"error": f"{type(exc).__name__}: {exc}"}, status_code=400
            )
        return JSONResponse(host.state_payload())

    async def open_session(request: Request) -> Response:
        if host.busy:
            return JSONResponse({"error": "stop the current turn first"}, status_code=409)
        body = await _json_object(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        session_id = str(body.get("id", "")).strip()
        if not session_id:
            return JSONResponse({"error": "id is required"}, status_code=400)
        try:
            await host.open_session(session_id)
        except Exception as exc:  # noqa: BLE001 - surfaced to the client
            return JSONResponse(
                {"error": f"{type(exc).__name__}: {exc}"}, status_code=400
            )
        return JSONResponse(host.state_payload())

    async def model(request: Request) -> Response:
        if host.busy:
            return JSONResponse({"error": "stop the current turn first"}, status_code=409)
        body = await _json_object(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        provider = str(body.get("provider", "")).strip()
        name = str(body.get("model", "")).strip()
        if not provider or not name:
            return JSONResponse({"error": "provider and model are required"}, status_code=400)
        try:
            await host.runtime.switch_model(provider, name)
        except Exception as exc:  # noqa: BLE001 - surfaced to the client
            return JSONResponse(
                {"error": f"{type(exc).__name__}: {exc}"}, status_code=400
            )
        return JSONResponse(host.state_payload())

    async def auto(request: Request) -> Response:
        body = await _json_object(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        enabled = bool(body.get("enabled", False))
        mode = host.runtime.set_auto(enabled)
        return JSONResponse({"permission_mode": mode})

    async def compact(request: Request) -> Response:
        if host.busy:
            return JSONResponse({"error": "stop the current turn first"}, status_code=409)
        events = [_event(event) async for event in host.runtime.compact()]
        return JSONResponse(
            {"events": events, "state": host.state_payload()}
        )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await host.close()

    routes = [
        Route("/", index),
        Route("/favicon.svg", favicon),
        Route("/api/bootstrap", bootstrap),
        Route("/api/health", health),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/permission", permission, methods=["POST"]),
        Route("/api/cancel", cancel, methods=["POST"]),
        Route("/api/clear", clear, methods=["POST"]),
        Route("/api/sessions", sessions),
        Route("/api/new", new_session, methods=["POST"]),
        Route("/api/open", open_session, methods=["POST"]),
        Route("/api/model", model, methods=["POST"]),
        Route("/api/auto", auto, methods=["POST"]),
        Route("/api/compact", compact, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=_security_headers)],
    )
    app.state.host = host
    return app


def serve(
    bundle: RuntimeBundle,
    asker: WebAsker,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    factory: BundleFactory | None = None,
) -> None:
    app = create_app(bundle, asker, factory)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    url = f"http://{host}:{port}/"
    if open_browser:
        def _open_when_ready() -> None:
            for _ in range(100):
                if server.started:
                    webbrowser.open(url)
                    return
                time.sleep(0.05)

        threading.Thread(target=_open_when_ready, daemon=True).start()
    print(f"cli-agent web: {url}", flush=True)
    server.run()
