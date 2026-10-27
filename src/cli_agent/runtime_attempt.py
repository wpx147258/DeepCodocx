"""One streamed provider attempt, normalized into agent events.

The runtime owns the loop and the transcript; this module owns a single model
request: text and reasoning deltas, tool-call announcements, the completed
message, and the error that ends the attempt. It never mutates the transcript.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from .errors import ProviderFailure
from .events import AgentEvent, AgentTextDelta, AgentToolStarted
from .messages import Message, StopReason, Usage
from .providers.base import (
    ModelRequest,
    Provider,
    ProviderError,
    ReasoningDelta,
    ResponseCompleted,
    TextDelta,
    ToolCall,
)


@dataclass
class Attempt:
    """Mutable state for one provider request."""

    assistant: Message | None = None
    stop_reason: StopReason = "end_turn"
    parse_errors: dict[str, str] = field(default_factory=dict)
    emitted_text: bool = False
    emitted_partial: bool = False
    usage: Usage = field(default_factory=Usage)


async def stream_attempt(
    provider: Provider, request: ModelRequest, attempt: Attempt
) -> AsyncIterator[AgentEvent]:
    """Stream one request, recording the outcome on ``attempt``.

    Provider failures surface as ``ProviderFailure`` so the loop can decide
    between retrying, compacting, and failing the turn.
    """
    async for event in provider.stream(request):
        if isinstance(event, TextDelta):
            if not event.text:
                continue
            attempt.emitted_text = True
            attempt.emitted_partial = True
            yield AgentTextDelta(event.text)
        elif isinstance(event, ReasoningDelta):
            if not event.text:
                continue
            attempt.emitted_partial = True
            yield AgentTextDelta(event.text, reasoning=True)
        elif isinstance(event, ToolCall):
            attempt.emitted_partial = True
            if event.parse_error:
                attempt.parse_errors[event.tool_use_id] = event.parse_error
            yield AgentToolStarted(event.tool_use_id, event.name, event.input)
        elif isinstance(event, ProviderError):
            raise ProviderFailure(event.message, event.kind, event.status_code)
        elif isinstance(event, ResponseCompleted):
            attempt.assistant = event.message
            attempt.stop_reason = event.stop_reason
            attempt.usage = event.usage
            if not attempt.emitted_text and event.message.text():
                # Providers that do not stream deltas still get live output.
                attempt.emitted_partial = True
                yield AgentTextDelta(event.message.text())
            # The completed message is terminal; trailing provider events are
            # noise and must not turn a finished response into an error.
            return
