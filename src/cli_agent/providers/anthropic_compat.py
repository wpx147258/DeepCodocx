"""Anthropic-compatible streaming adapter.

This is the only module that knows the Anthropic wire format. It translates
SSE events into normalized ``ProviderEvent``s and builds provider-neutral
messages for the runtime.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

import anthropic
from anthropic import AsyncAnthropic

from ..messages import (
    ContentBlock,
    ImageBlock,
    Message,
    ReasoningBlock,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    new_id,
    now_iso,
)
from .base import (
    ErrorKind,
    ModelProfile,
    ModelRequest,
    ProviderError,
    ProviderEvent,
    ReasoningDelta,
    ResponseCompleted,
    TextDelta,
    ToolCall,
)

logger = logging.getLogger(__name__)


def _stop_reason(raw: str | None) -> tuple[StopReason, str | None]:
    if raw in (None, "end_turn", "stop_sequence"):
        return "end_turn", raw
    if raw == "tool_use":
        return "tool_use", raw
    if raw == "max_tokens":
        return "max_tokens", raw
    if raw in ("refusal", "content_filter"):
        return "refusal", raw
    return "error", raw


def _classify_error(exc: BaseException) -> tuple[str, int | None]:
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        text = str(exc).lower()
        if status in (429, 529) or status >= 500:
            return "retryable", status
        if status == 413 or "prompt is too long" in text or "context length" in text:
            return "context_length", status
        if status in (401, 403):
            return "auth", status
        return "invalid_request", status
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return "retryable", None
    return "fatal", None


class AnthropicCompatProvider:
    name = "anthropic_compat"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 300.0,
        client: Any | None = None,
    ) -> None:
        self._client = client or AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )
        # Third-party Anthropic-compatible hosts usually expect x-api-key.
        # The SDK may also read ANTHROPIC_AUTH_TOKEN from the environment as a
        # bearer token; clear it when we are pointed at a custom host.
        if client is None and base_url and getattr(self._client, "auth_token", None):
            self._client.auth_token = None

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]:
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "messages": _messages_to_wire(request.messages, request.profile),
        }
        system = "\n\n".join(section.text for section in request.system if section.text)
        if system:
            params["system"] = system
        if request.tools:
            params["tools"] = [_tool_to_wire(tool) for tool in request.tools]
        if request.thinking is not None:
            # Extended thinking and temperature are mutually exclusive.
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": request.thinking.budget_tokens,
            }
        elif request.temperature is not None:
            params["temperature"] = request.temperature

        try:
            async with self._client.messages.stream(**params) as stream:
                async for event in stream:
                    event_type = getattr(event, "type", "")
                    if event_type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is None:
                            continue
                        delta_type = getattr(delta, "type", "")
                        if delta_type == "text_delta":
                            yield TextDelta(delta.text)
                        elif delta_type == "thinking_delta":
                            yield ReasoningDelta(delta.thinking)
                        continue
                    if event_type == "content_block_stop":
                        block = getattr(event, "content_block", None)
                        if block is not None and getattr(block, "type", "") == "tool_use":
                            raw_input = getattr(block, "input", None)
                            parse_error = None
                            if not isinstance(raw_input, Mapping):
                                parse_error = "tool input was not a JSON object"
                                raw_input = {}
                            yield ToolCall(
                                tool_use_id=str(getattr(block, "id", "")),
                                name=str(getattr(block, "name", "")),
                                input=dict(raw_input),
                                parse_error=parse_error,
                            )
                        continue
                    # Raw message_start/message_delta/message_stop and the SDK's
                    # high-level TextEvent/InputJsonEvent/ThinkingEvent/
                    # SignatureEvent are intentionally ignored. We use deltas
                    # for streaming display and the final message for truth.

                final = await stream.get_final_message()
        except Exception as exc:  # noqa: BLE001 - mapped into a typed event
            kind, status = _classify_error(exc)
            message = f"{type(exc).__name__}: {exc}"
            if kind == "retryable":
                # The caller may retry. Do not turn a transient hiccup into a
                # user-facing warning; log at INFO and let the retry layer decide.
                logger.info("provider error (retryable): %s", message)
            else:
                logger.warning("provider error (%s): %s", kind, message)
            yield ProviderError(
                message=message,
                kind=cast(ErrorKind, kind),
                status_code=status,
            )
            return

        blocks = _final_blocks(final)
        stop_reason, raw_stop_reason = _stop_reason(getattr(final, "stop_reason", None))
        usage = _final_usage(final)
        yield ResponseCompleted(
            message=Message(
                id=new_id(),
                role="assistant",
                content=tuple(blocks),
                created_at=now_iso(),
            ),
            stop_reason=stop_reason,
            raw_stop_reason=raw_stop_reason,
            usage=usage,
        )


def _messages_to_wire(
    messages: tuple[Message, ...], profile: ModelProfile | None
) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for message in messages:
        content = _content_to_wire(message.content, profile)
        if not content:
            content = [{"type": "text", "text": " "}]
        wire.append({"role": message.role, "content": content})
    return wire


def _content_to_wire(
    content: tuple[ContentBlock, ...], profile: ModelProfile | None
) -> list[dict[str, Any]]:
    replay_reasoning = bool(getattr(profile, "replay_reasoning", False))
    supports_images = bool(getattr(profile, "supports_images", False))
    wire: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            wire.append({"type": "text", "text": block.text})
        elif isinstance(block, ReasoningBlock):
            if replay_reasoning:
                redacted = block.opaque.get("redacted")
                signature = block.opaque.get("signature", "")
                if redacted:
                    wire.append(
                        {"type": "redacted_thinking", "data": str(redacted)}
                    )
                elif signature:
                    wire.append(
                        {
                            "type": "thinking",
                            "thinking": block.text,
                            "signature": str(signature),
                        }
                    )
        elif isinstance(block, ImageBlock):
            if supports_images:
                wire.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": block.media_type,
                            "data": block.data,
                        },
                    }
                )
            else:
                wire.append(
                    {"type": "text", "text": "[image omitted: provider profile does not support images]"}
                )
        elif isinstance(block, ToolUseBlock):
            wire.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input),
                }
            )
        elif isinstance(block, ToolResultBlock):
            result_content = [
                _result_content_to_wire(item, supports_images)
                for item in block.content
            ]
            if not result_content:
                result_content = [{"type": "text", "text": "(no output)"}]
            wire.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": result_content,
                    "is_error": block.is_error,
                }
            )
    return wire


def _result_content_to_wire(
    block: TextBlock | ImageBlock, supports_images: bool
) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if supports_images:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": block.media_type,
                "data": block.data,
            },
        }
    return {
        "type": "text",
        "text": "[image omitted: provider profile does not support images]",
    }


def _tool_to_wire(tool: Any) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.input_schema),
    }


def _final_blocks(final: Any) -> list[ContentBlock]:
    blocks: list[ContentBlock] = []
    for block in getattr(final, "content", ()) or ():
        block_type = getattr(block, "type", "")
        if block_type == "text":
            blocks.append(TextBlock(str(getattr(block, "text", ""))))
        elif block_type == "tool_use":
            raw_input = getattr(block, "input", {})
            if not isinstance(raw_input, Mapping):
                raw_input = {}
            blocks.append(
                ToolUseBlock(
                    id=str(getattr(block, "id", "")),
                    name=str(getattr(block, "name", "")),
                    input=dict(raw_input),
                )
            )
        elif block_type == "thinking":
            blocks.append(
                ReasoningBlock(
                    str(getattr(block, "thinking", "")),
                    {"signature": getattr(block, "signature", "")},
                )
            )
        elif block_type == "redacted_thinking":
            blocks.append(
                ReasoningBlock("", {"redacted": getattr(block, "data", "")})
            )
        else:
            logger.debug("ignoring unknown assistant block type: %s", block_type)
    return blocks


def _final_usage(final: Any) -> Usage:
    usage = getattr(final, "usage", None)
    if usage is None:
        return Usage()
    return Usage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )
