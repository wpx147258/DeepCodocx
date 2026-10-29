"""Scripted provider used by loop tests. No network."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any

from dcx.messages import (
    Message,
    StopReason,
    TextBlock,
    ToolUseBlock,
    Usage,
    assistant_message,
)
from dcx.providers.base import (
    ModelRequest,
    ProviderError,
    ProviderEvent,
    ResponseCompleted,
)


def text_response(
    text: str,
    *,
    usage: Usage | None = None,
    stop_reason: StopReason = "end_turn",
) -> ResponseCompleted:
    return ResponseCompleted(
        message=assistant_message([TextBlock(text)]),
        stop_reason=stop_reason,
        raw_stop_reason=stop_reason,
        usage=usage or Usage(input_tokens=10, output_tokens=5),
    )


def tool_response(
    calls: Iterable[tuple[str, str, Mapping[str, Any]]],
    *,
    text: str | None = None,
    usage: Usage | None = None,
) -> ResponseCompleted:
    blocks: list[Any] = []
    if text:
        blocks.append(TextBlock(text))
    for tool_use_id, name, tool_input in calls:
        blocks.append(ToolUseBlock(tool_use_id, name, dict(tool_input)))
    return ResponseCompleted(
        message=assistant_message(blocks),
        stop_reason="tool_use",
        raw_stop_reason="tool_use",
        usage=usage or Usage(input_tokens=10, output_tokens=5),
    )


class FakeProvider:
    def __init__(self, scripts: list[list[ProviderEvent]]) -> None:
        self.scripts = list(scripts)
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        if not self.scripts:
            raise AssertionError("FakeProvider ran out of scripted responses")
        for event in self.scripts.pop(0):
            yield event

    async def aclose(self) -> None:
        self.closed = True


def fail(message: str = "provider failed", kind: str = "fatal") -> ProviderError:
    return ProviderError(message, kind)  # type: ignore[arg-type]


__all__ = ["FakeProvider", "fail", "text_response", "tool_response", "Message"]
