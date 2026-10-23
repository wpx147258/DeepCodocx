"""Provider-neutral message and content-block types.

Everything inside the runtime speaks this vocabulary. Vendor wire formats are
translated in ``providers/`` and nowhere else.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

type Role = Literal["user", "assistant"]
type StopReason = Literal[
    "end_turn", "tool_use", "max_tokens", "refusal", "error", "max_turns"
]


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningBlock:
    """A provider reasoning block.

    ``opaque`` preserves provider-specific fields (for example a signature).
    It is persisted with the transcript but is not replayed unless the model
    profile explicitly asks for it.
    """

    text: str
    opaque: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ImageBlock:
    media_type: str
    data: str  # base64


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: tuple[TextBlock | ImageBlock, ...]
    is_error: bool = False


type AssistantContentBlock = TextBlock | ReasoningBlock | ImageBlock | ToolUseBlock
type ContentBlock = AssistantContentBlock | ToolResultBlock


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    role: Role
    content: tuple[ContentBlock, ...]
    created_at: str

    def text(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextBlock))

    def tool_uses(self) -> tuple[ToolUseBlock, ...]:
        return tuple(block for block in self.content if isinstance(block, ToolUseBlock))

    def tool_results(self) -> tuple[ToolResultBlock, ...]:
        return tuple(block for block in self.content if isinstance(block, ToolResultBlock))

    def has_tool_use(self) -> bool:
        return any(isinstance(block, ToolUseBlock) for block in self.content)


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


def new_id(prefix: str = "msg") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def user_message(text: str, *, id: str | None = None) -> Message:
    return Message(
        id=id or new_id(),
        role="user",
        content=(TextBlock(text),),
        created_at=now_iso(),
    )


def assistant_message(
    content: tuple[ContentBlock, ...] | list[ContentBlock],
    *,
    id: str | None = None,
) -> Message:
    return Message(
        id=id or new_id(),
        role="assistant",
        content=tuple(content),
        created_at=now_iso(),
    )


def tool_result_message(
    results: list[ToolResultBlock] | tuple[ToolResultBlock, ...],
    *,
    id: str | None = None,
) -> Message:
    return Message(
        id=id or new_id(),
        role="user",
        content=tuple(results),
        created_at=now_iso(),
    )


def text_result(text: str) -> tuple[TextBlock, ...]:
    return (TextBlock(text),)


def text_of(content: tuple[TextBlock | ImageBlock, ...]) -> str:
    return "".join(block.text for block in content if isinstance(block, TextBlock))


# ---------------------------------------------------------------------------
# JSON round-trip (session persistence and golden tests)
# ---------------------------------------------------------------------------


def content_block_to_dict(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ReasoningBlock):
        return {"type": "reasoning", "text": block.text, "opaque": dict(block.opaque)}
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
            "content": [content_block_to_dict(item) for item in block.content],
            "is_error": block.is_error,
        }
    raise TypeError(f"unknown content block: {type(block)!r}")


def content_block_from_dict(data: Mapping[str, Any]) -> ContentBlock:
    kind = data.get("type")
    if kind == "text":
        return TextBlock(str(data.get("text", "")))
    if kind == "reasoning":
        opaque = data.get("opaque") or {}
        return ReasoningBlock(str(data.get("text", "")), dict(opaque))
    if kind == "image":
        return ImageBlock(str(data.get("media_type", "")), str(data.get("data", "")))
    if kind == "tool_use":
        raw_input = data.get("input") or {}
        return ToolUseBlock(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            input=dict(raw_input),
        )
    if kind == "tool_result":
        raw_content = data.get("content") or []
        result_content: list[TextBlock | ImageBlock] = []
        for item in raw_content:
            parsed = content_block_from_dict(item)
            if not isinstance(parsed, (TextBlock, ImageBlock)):
                raise ValueError("tool_result may contain only text and image blocks")
            result_content.append(parsed)
        return ToolResultBlock(
            tool_use_id=str(data.get("tool_use_id", "")),
            content=tuple(result_content),
            is_error=bool(data.get("is_error", False)),
        )
    raise ValueError(f"unknown content block type: {kind!r}")


def message_to_dict(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "role": message.role,
        "content": [content_block_to_dict(block) for block in message.content],
        "created_at": message.created_at,
    }


def message_from_dict(data: Mapping[str, Any]) -> Message:
    role = data.get("role")
    if role not in ("user", "assistant"):
        raise ValueError(f"invalid message role: {role!r}")
    raw_content = data.get("content") or []
    return Message(
        id=str(data.get("id", new_id())),
        role=role,
        content=tuple(content_block_from_dict(item) for item in raw_content),
        created_at=str(data.get("created_at", now_iso())),
    )


def usage_to_dict(usage: Usage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }


def usage_from_dict(data: Mapping[str, Any]) -> Usage:
    return Usage(
        input_tokens=int(data.get("input_tokens", 0)),
        output_tokens=int(data.get("output_tokens", 0)),
        cache_read_tokens=int(data.get("cache_read_tokens", 0)),
        cache_write_tokens=int(data.get("cache_write_tokens", 0)),
    )
