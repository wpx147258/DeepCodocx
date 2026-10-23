"""Single tool executor: validate -> permission -> run -> normalize errors."""

from __future__ import annotations

import asyncio
import logging

from ..messages import ToolUseBlock
from ..permissions.policy import Asker, PermissionPolicy
from .base import ToolContext, ToolResult
from .registry import ToolRegistry
from .validate import validate_tool_args

logger = logging.getLogger(__name__)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(
        self,
        call: ToolUseBlock,
        *,
        ctx: ToolContext,
        policy: PermissionPolicy,
        asker: Asker | None,
        parse_error: str | None = None,
    ) -> ToolResult:
        if parse_error:
            return ToolResult.error(
                f"Error: could not parse the arguments for tool {call.name!r}: {parse_error}"
            )

        tool = self.registry.get(call.name)
        if tool is None:
            available = ", ".join(spec.name for spec in self.registry.specs())
            return ToolResult.error(
                f"Error: unknown tool {call.name!r}. Available tools: {available}"
            )

        validation_error = validate_tool_args(tool.spec, call.input)
        if validation_error:
            return ToolResult.error(f"Error: {validation_error}")

        outcome = await policy.authorize(tool.spec, call.input, asker)
        if not outcome.allowed:
            reason = outcome.reason or "permission denied"
            return ToolResult.error(
                f"Permission denied: {reason}. "
                "Do not repeat the same call without changing your approach."
            )

        try:
            result = await tool.run(call.input, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool failure is data
            logger.exception("tool %s failed", call.name)
            return ToolResult.error(f"Error: {type(exc).__name__}: {exc}")

        if not isinstance(result, ToolResult):
            return ToolResult.error(
                f"Error: tool {call.name!r} returned an invalid result"
            )
        return result
