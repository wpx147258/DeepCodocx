"""Canonical transcript and the single request-repair boundary.

The runtime stores messages exactly as they happened. ``for_request`` produces a
defensive view for the provider: orphaned tool calls and results are repaired,
roles alternate, and the conversation starts with a user message. No other module
repairs messages.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .messages import (
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    new_id,
    now_iso,
    user_message,
)

MISSING_TOOL_RESULT = "[Tool result missing due to internal error]"
_NEUTRAL_USER = "Continue."


class Transcript:
    def __init__(self, messages: Iterable[Message] = ()) -> None:
        self._messages: list[Message] = list(messages)

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def append(self, message: Message) -> None:
        self._messages.append(message)

    def extend(self, messages: Iterable[Message]) -> None:
        self._messages.extend(messages)

    def replace(self, messages: Iterable[Message]) -> None:
        self._messages = list(messages)

    def clear(self) -> None:
        self._messages.clear()

    def index_of(self, message_id: str) -> int | None:
        for index, message in enumerate(self._messages):
            if message.id == message_id:
                return index
        return None

    def tail_from(self, message_id: str) -> list[Message]:
        index = self.index_of(message_id)
        return [] if index is None else self._messages[index:]

    def for_request(self) -> list[Message]:
        """Return a repaired, provider-ready view without mutating the transcript."""
        if not self._messages:
            return []

        repaired: list[Message] = []
        pending_tool_ids: set[str] | None = None

        for message in self._messages:
            if message.role == "assistant":
                if pending_tool_ids:
                    repaired.append(_synthetic_result_message(pending_tool_ids))
                repaired.append(message)
                pending_tool_ids = {block.id for block in message.tool_uses()} or None
                continue

            # user message
            results = [block for block in message.content if isinstance(block, ToolResultBlock)]
            other = [block for block in message.content if not isinstance(block, ToolResultBlock)]

            if pending_tool_ids is None:
                # Orphan results from a lost assistant turn: drop them but keep text.
                blocks: list[ContentBlock] = list(other)
            else:
                valid: list[ToolResultBlock] = []
                seen: set[str] = set()
                for block in results:
                    if block.tool_use_id in pending_tool_ids and block.tool_use_id not in seen:
                        valid.append(block)
                        seen.add(block.tool_use_id)
                missing = pending_tool_ids - seen
                synthetic = [_synthetic_result(tool_id) for tool_id in sorted(missing)]
                blocks = [*valid, *synthetic, *other]
                pending_tool_ids = None

            if blocks:
                repaired.append(
                    Message(
                        id=message.id,
                        role="user",
                        content=tuple(blocks),
                        created_at=message.created_at,
                    )
                )

        if pending_tool_ids:
            repaired.append(_synthetic_result_message(pending_tool_ids))

        return _normalize_roles(repaired)


def _synthetic_result(tool_use_id: str) -> ToolResultBlock:
    return ToolResultBlock(
        tool_use_id=tool_use_id,
        content=(TextBlock(MISSING_TOOL_RESULT),),
        is_error=True,
    )


def _synthetic_result_message(tool_ids: set[str]) -> Message:
    return Message(
        id=new_id(),
        role="user",
        content=tuple(_synthetic_result(tool_id) for tool_id in sorted(tool_ids)),
        created_at=now_iso(),
    )


def _merge_user_messages(first: Message, second: Message) -> Message:
    return Message(
        id=first.id,
        role="user",
        content=(*first.content, *second.content),
        created_at=first.created_at,
    )


def _normalize_roles(messages: Sequence[Message]) -> list[Message]:
    merged: list[Message] = []
    for message in messages:
        if merged and merged[-1].role == message.role:
            if message.role == "user":
                merged[-1] = _merge_user_messages(merged[-1], message)
                continue
            # Consecutive assistant turns are not valid for the API. Insert a
            # neutral user turn between them.
            merged.append(user_message(_NEUTRAL_USER))
        merged.append(message)

    if merged and merged[0].role != "user":
        merged.insert(0, user_message(_NEUTRAL_USER))

    return merged
