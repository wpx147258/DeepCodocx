"""Provider protocol and normalized model events."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..messages import Message, StopReason, Usage
from ..tools.base import ToolSpec


@dataclass(frozen=True, slots=True)
class ThinkingConfig:
    """Request extended thinking for providers that support it.

    The budget is the maximum number of tokens the model may spend on reasoning.
    It must be smaller than the request's ``max_output_tokens``.
    """

    budget_tokens: int = 4_096


@dataclass(frozen=True, slots=True)
class ModelProfile:
    provider_name: str
    provider_kind: Literal["anthropic_compat", "openai_compat"]
    model: str
    context_window: int
    max_output_tokens: int
    supports_tools: bool = True
    supports_images: bool = False
    supports_thinking: bool = False
    replay_reasoning: bool = False


@dataclass(frozen=True, slots=True)
class SystemSection:
    name: str
    text: str
    cacheable: bool = False


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model: str
    system: tuple[SystemSection, ...]
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    max_output_tokens: int
    profile: ModelProfile | None = None
    thinking: ThinkingConfig | None = None
    temperature: float | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool_use_id: str
    name: str
    input: Mapping[str, Any]
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class ResponseCompleted:
    message: Message
    stop_reason: StopReason
    raw_stop_reason: str | None
    usage: Usage


type ErrorKind = Literal[
    "retryable", "context_length", "auth", "invalid_request", "fatal"
]


@dataclass(frozen=True, slots=True)
class ProviderError:
    message: str
    kind: ErrorKind
    status_code: int | None = None


type ProviderEvent = TextDelta | ReasoningDelta | ToolCall | ResponseCompleted | ProviderError


class Provider(Protocol):
    name: str

    def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]: ...

    async def aclose(self) -> None: ...
