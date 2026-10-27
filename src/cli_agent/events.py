"""Events emitted by the runtime and consumed by CLI renderers and tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .messages import ImageBlock, StopReason, TextBlock, Usage


@dataclass(frozen=True, slots=True)
class AgentTextDelta:
    text: str
    reasoning: bool = False


@dataclass(frozen=True, slots=True)
class AgentRetry:
    """A failed provider attempt is being retried.

    ``partial`` means text had already been streamed to the host, so whatever is
    displayed next repeats the abandoned attempt rather than continuing it.
    """

    attempt: int
    max_attempts: int
    kind: str
    reason: str
    partial: bool = False


@dataclass(frozen=True, slots=True)
class AgentToolStarted:
    tool_use_id: str
    name: str
    input: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AgentToolFinished:
    tool_use_id: str
    name: str
    content: tuple[TextBlock | ImageBlock, ...]
    is_error: bool


@dataclass(frozen=True, slots=True)
class AgentUsage:
    turn: Usage
    total: Usage


@dataclass(frozen=True, slots=True)
class AgentContextEvent:
    kind: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AgentNotice:
    text: str


@dataclass(frozen=True, slots=True)
class AgentError:
    message: str
    kind: str
    status_code: int | None = None


@dataclass(frozen=True, slots=True)
class AgentTurnFinished:
    stop_reason: StopReason
    total: Usage


type AgentEvent = (
    AgentTextDelta
    | AgentRetry
    | AgentToolStarted
    | AgentToolFinished
    | AgentUsage
    | AgentContextEvent
    | AgentNotice
    | AgentError
    | AgentTurnFinished
)
