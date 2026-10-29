"""Small, bounded MCP stdio client. No model or agent loop lives here."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from ..messages import ImageBlock, TextBlock
from ..tools.base import ToolResult

logger = logging.getLogger(__name__)
MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    read_tools: tuple[str, ...] = ()
    timeout: float = 30.0


class StdioClient:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._counter = 0
        self._ready = False

    async def _start(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        await self._close()
        self._process = await asyncio.create_subprocess_exec(
            *self.config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **self.config.env},
            limit=MAX_FRAME_BYTES,
            start_new_session=os.name != "nt",
        )
        assert self._process.stderr is not None
        self._stderr = asyncio.create_task(self._drain_stderr(self._process.stderr))
        await self._exchange(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cli-agent", "version": "0.1.0"},
            },
        )
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._ready = True

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(4096):
            logger.debug("MCP %s: %s", self.config.name, chunk.decode(errors="replace")[:4096])

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        self._process.stdin.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
        await self._process.stdin.drain()

    async def _exchange(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._counter += 1
        request_id = self._counter
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        assert self._process is not None and self._process.stdout is not None
        for _ in range(1000):
            line = await self._process.stdout.readline()
            if not line:
                raise RuntimeError(f"MCP server {self.config.name!r} disconnected")
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("MCP response must be an object")
            if "method" in payload:
                if "id" in payload:
                    await self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": payload["id"],
                            "error": {
                                "code": -32601,
                                "message": "client capability is not supported",
                            },
                        }
                    )
                continue
            if payload.get("id") != request_id:
                continue
            if "error" in payload:
                raise RuntimeError(f"MCP {self.config.name}: {payload['error']}")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise ValueError("MCP result must be an object")
            return result
        raise RuntimeError("MCP notification limit exceeded")

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            try:
                async with asyncio.timeout(self.config.timeout):
                    await self._start()
                    return await self._exchange(method, params or {})
            except BaseException:
                await self._close()
                raise

    async def tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(50):
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools", [])
            if not isinstance(page, list):
                raise ValueError("MCP tools must be an array")
            tools.extend(item for item in page if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools[:1000]
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError("MCP tools pagination did not advance")
            seen.add(cursor)
        raise ValueError("MCP tool directory is too large")

    async def close(self) -> None:
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        process, self._process = self._process, None
        self._ready = False
        if process is not None:
            if process.stdin:
                process.stdin.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 0.5)
            # A peer can exit while its children still own pipes. Clean up the
            # process group even after the group leader exits.
            with contextlib.suppress(ProcessLookupError):
                if os.name == "nt":
                    if process.returncode is None:
                        process.kill()
                else:
                    import signal

                    os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        task, self._stderr = self._stderr, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def normalize_result(payload: dict[str, Any]) -> ToolResult:
    blocks: list[TextBlock | ImageBlock] = []
    total_images = 0
    remaining_text = 256_000
    content = payload.get("content", [])
    if not isinstance(content, list):
        raise ValueError("MCP content must be an array")
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            text = str(item.get("text", ""))
            if remaining_text:
                blocks.append(
                    TextBlock(
                        text[:remaining_text]
                        + ("\n[MCP text truncated]" if len(text) > remaining_text else "")
                    )
                )
                remaining_text = max(0, remaining_text - len(text))
        elif kind == "image":
            mime, data = item.get("mimeType"), item.get("data")
            if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"} or not isinstance(
                data, str
            ):
                raise ValueError("MCP image has an unsupported format")
            if len(data) > MAX_IMAGE_BYTES * 4 // 3 + 4:
                raise ValueError("MCP image exceeds 5 MiB")
            raw = base64.b64decode(data, validate=True)
            total_images += len(raw)
            if len(raw) > MAX_IMAGE_BYTES or total_images > 12 * 1024 * 1024:
                raise ValueError("MCP image budget exceeded")
            blocks.append(ImageBlock(str(mime), data))
        else:
            blocks.append(
                TextBlock(f"[MCP content {kind!r}: {json.dumps(item, ensure_ascii=False)[:2000]}]")
            )
    if not blocks and "structuredContent" in payload:
        blocks.append(
            TextBlock(json.dumps(payload["structuredContent"], ensure_ascii=False)[:256_000])
        )
    return ToolResult(tuple(blocks) or (TextBlock("(no output)"),), bool(payload.get("isError")))
