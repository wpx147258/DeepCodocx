"""Single tool executor: validate -> permission -> run -> normalize errors.

``prepare`` and ``run`` are separate so the runtime can authorize every call in
model order (one interactive prompt at a time) and then run concurrency-safe
reads in parallel.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass

from ..messages import ToolUseBlock
from ..permissions.policy import Asker, PermissionPolicy
from .base import Tool, ToolContext, ToolResult
from .registry import ToolRegistry
from .validate import validate_tool_args

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedCall:
    """A tool call that has passed parse, validation, and permission checks."""

    call: ToolUseBlock
    tool: Tool | None = None
    early_error: ToolResult | None = None

    @property
    def runnable(self) -> bool:
        return self.early_error is None and self.tool is not None


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def prepare(
        self,
        call: ToolUseBlock,
        *,
        policy: PermissionPolicy,
        asker: Asker | None,
        parse_error: str | None = None,
    ) -> PreparedCall:
        if parse_error:
            return PreparedCall(
                call,
                early_error=ToolResult.error(
                    f"Error: could not parse the arguments for tool {call.name!r}: {parse_error}"
                ),
            )

        tool = self.registry.get(call.name)
        if tool is None:
            available = ", ".join(spec.name for spec in self.registry.specs())
            return PreparedCall(
                call,
                early_error=ToolResult.error(
                    f"Error: unknown tool {call.name!r}. Available tools: {available}"
                ),
            )

        validation_error = validate_tool_args(tool.spec, call.input)
        if validation_error:
            return PreparedCall(call, early_error=ToolResult.error(f"Error: {validation_error}"))

        outcome = await policy.authorize(tool.spec, call.input, asker)
        if not outcome.allowed:
            reason = outcome.reason or "permission denied"
            return PreparedCall(
                call,
                early_error=ToolResult.error(
                    f"Permission denied: {reason}. "
                    "Do not repeat the same call without changing your approach."
                ),
            )
        return PreparedCall(call, tool=tool)

    async def run(self, prepared: PreparedCall, *, ctx: ToolContext) -> ToolResult:
        if prepared.early_error is not None:
            return prepared.early_error
        tool = prepared.tool
        if tool is None:  # pragma: no cover - guarded by PreparedCall.runnable
            return ToolResult.error("Error: tool was not prepared")
        try:
            result = await tool.run(prepared.call.input, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool failure is data
            logger.exception("tool %s failed", prepared.call.name)
            return ToolResult.error(f"Error: {type(exc).__name__}: {exc}")

        if not isinstance(result, ToolResult):
            return ToolResult.error(
                f"Error: tool {prepared.call.name!r} returned an invalid result"
            )
        return result

    async def execute(
        self,
        call: ToolUseBlock,
        *,
        ctx: ToolContext,
        policy: PermissionPolicy,
        asker: Asker | None,
        parse_error: str | None = None,
    ) -> ToolResult:
        prepared = await self.prepare(
            call, policy=policy, asker=asker, parse_error=parse_error
        )
        return await self.run(prepared, ctx=ctx)

    async def run_all(
        self, prepared: Sequence[PreparedCall], *, ctx: ToolContext
    ) -> AsyncIterator[tuple[PreparedCall, ToolResult]]:
        """Run calls in order, parallelizing consecutive concurrency-safe ones."""
        for batch in _batches(prepared):
            if len(batch) == 1:
                yield batch[0], await self.run(batch[0], ctx=ctx)
                continue
            results = await asyncio.gather(*(self.run(item, ctx=ctx) for item in batch))
            for item, result in zip(batch, results, strict=True):
                yield item, result


def _batches(prepared: Sequence[PreparedCall]) -> Iterator[list[PreparedCall]]:
    """Group consecutive concurrency-safe calls; anything else is a barrier."""
    batch: list[PreparedCall] = []
    for item in prepared:
        if item.runnable and item.tool is not None and item.tool.spec.concurrency_safe:
            batch.append(item)
            continue
        if batch:
            yield batch
            batch = []
        yield [item]
    if batch:
        yield batch
