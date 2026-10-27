"""Small, deliberately approximate token accounting.

The estimate is used for compaction thresholds only. It is calibrated against
provider-reported input tokens after each response; reactive compaction covers
the case where the estimate is still wrong.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ..messages import (
    ImageBlock,
    Message,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from ..providers.base import SystemSection
from ..tools.base import ToolSpec

_IMAGE_TOKENS = 1_600
_MESSAGE_OVERHEAD = 4


def estimate_text(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    other_chars = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + int(other_chars * 1.2))


def estimate_block(block: Any) -> int:
    if isinstance(block, TextBlock):
        return estimate_text(block.text)
    if isinstance(block, ReasoningBlock):
        return estimate_text(block.text)
    if isinstance(block, ImageBlock):
        return _IMAGE_TOKENS
    if isinstance(block, ToolUseBlock):
        return estimate_text(block.name) + estimate_text(json.dumps(dict(block.input), ensure_ascii=False))
    if isinstance(block, ToolResultBlock):
        return sum(estimate_block(item) for item in block.content)
    return 0


def estimate_message(message: Message) -> int:
    return _MESSAGE_OVERHEAD + sum(estimate_block(block) for block in message.content)


def prompt_tokens(usage: Usage) -> int:
    """Prompt size as the provider counted it.

    Anthropic-style APIs report the cached prefix separately from
    ``input_tokens``; both are part of the prompt. Treating ``input_tokens``
    alone as the prompt size undercounts by orders of magnitude once caching is
    warm, which then corrupts calibration and any context readout.
    """
    return usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens


class TokenEstimator:
    def __init__(self, factor: float = 1.0) -> None:
        self.factor = factor

    def estimate_text(self, text: str) -> int:
        return max(0, int(estimate_text(text) * self.factor))

    def estimate_messages(self, messages: tuple[Message, ...] | list[Message]) -> int:
        return max(0, int(sum(estimate_message(message) for message in messages) * self.factor))

    def estimate_request(
        self,
        messages: Sequence[Message],
        system: tuple[SystemSection, ...] = (),
        tools: tuple[ToolSpec, ...] = (),
    ) -> int:
        """Estimate everything the provider counts as input.

        System sections and tool schemas are sent on every request, so leaving
        them out makes ``calibrate`` blame the transcript for a constant overhead.
        """
        raw = sum(estimate_message(message) for message in messages)
        raw += sum(estimate_text(section.text) for section in system)
        for tool in tools:
            raw += estimate_text(tool.name) + estimate_text(tool.description)
            raw += estimate_text(json.dumps(dict(tool.input_schema), ensure_ascii=False))
        return max(0, int(raw * self.factor))

    def calibrate(self, estimated: int, actual: int) -> None:
        """Blend the current factor toward the provider's observed token count."""
        if estimated <= 0 or actual <= 0:
            return
        ratio = actual / estimated
        # Avoid chasing one unusual response.
        ratio = min(max(ratio, 0.5), 2.0)
        self.factor = min(2.0, max(0.5, self.factor * 0.8 + ratio * 0.2))
