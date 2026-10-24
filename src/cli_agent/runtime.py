"""The one agent loop.

``AgentRuntime`` owns the canonical transcript, the active provider/profile, and
cumulative usage. Hosts only submit user text and consume events.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace

from .context.memory import ExtractionCoordinator, MemoryExtractor, MemoryStore
from .context.pipeline import ContextPipeline
from .context.prompts import PromptBuilder, PromptContext
from .context.skills import SkillLoader
from .context.tokens import TokenEstimator
from .errors import ConfigError, ProviderFailure
from .events import (
    AgentContextEvent,
    AgentError,
    AgentEvent,
    AgentNotice,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnFinished,
    AgentUsage,
)
from .messages import (
    Message,
    StopReason,
    TextBlock,
    ToolResultBlock,
    Usage,
    tool_result_message,
    user_message,
)
from .permissions.policy import Asker, PermissionPolicy
from .providers.base import (
    ModelProfile,
    ModelRequest,
    Provider,
    ProviderError,
    ReasoningDelta,
    ResponseCompleted,
    TextDelta,
    ThinkingConfig,
    ToolCall,
)
from .session.store import SessionStore
from .tools.base import ToolContext, ToolResult
from .tools.execution import ToolExecutor
from .tools.registry import ToolRegistry
from .transcript import Transcript

logger = logging.getLogger(__name__)

CONTINUE_PROMPT = "Continue from where you left off. Do not repeat completed work."
CANCELLED_RESULT = "[Tool call cancelled by the user]"
MAX_RETRIES = 3
MAX_CONTINUATIONS = 2

ProviderBuild = tuple[Provider, ModelProfile, ThinkingConfig | None]


@dataclass
class ProviderSlot:
    """Mutable current-provider holder shared with background helpers."""

    provider: Provider
    profile: ModelProfile
    thinking: ThinkingConfig | None = None

    def replace(
        self,
        provider: Provider,
        profile: ModelProfile,
        thinking: ThinkingConfig | None,
    ) -> None:
        self.provider = provider
        self.profile = profile
        self.thinking = thinking


@dataclass
class RuntimeDeps:
    provider: Provider
    profile: ModelProfile
    transcript: Transcript
    registry: ToolRegistry
    executor: ToolExecutor
    policy: PermissionPolicy
    asker: Asker | None
    context: ContextPipeline
    prompt_builder: PromptBuilder
    session: SessionStore
    tool_context: ToolContext
    skills: SkillLoader
    project_instructions: str
    estimator: TokenEstimator
    initial_usage: Usage = field(default_factory=Usage)
    max_turns: int = 50
    memory: MemoryStore | None = None
    memory_enabled: bool = True
    extraction: ExtractionCoordinator | None = None
    extractor: MemoryExtractor | None = None
    provider_factory: Callable[[str, str], ProviderBuild] | None = None
    thinking: ThinkingConfig | None = None
    provider_slot: ProviderSlot | None = None


@dataclass
class _Recovery:
    retries: int = 0
    continuations: int = 0
    attempted_reactive: bool = False
    reactive_requested: bool = False


class AgentRuntime:
    def __init__(self, deps: RuntimeDeps) -> None:
        self._deps = deps
        if deps.provider_slot is None:
            deps.provider_slot = ProviderSlot(
                deps.provider, deps.profile, deps.thinking
            )
        self._usage = deps.initial_usage
        self._active_request = ""
        self._extraction_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # public surface
    # ------------------------------------------------------------------

    @property
    def messages(self) -> tuple[Message, ...]:
        return self._deps.transcript.messages

    @property
    def usage(self) -> Usage:
        return self._usage

    @property
    def model(self) -> str:
        return self._deps.profile.model

    @property
    def provider_name(self) -> str:
        return self._deps.profile.provider_name

    @property
    def has_pending_extraction(self) -> bool:
        return self._extraction_task is not None and not self._extraction_task.done()

    @property
    def thinking(self) -> ThinkingConfig | None:
        return self._deps.thinking

    async def run_turn(self, text: str | None = None) -> AsyncIterator[AgentEvent]:
        if text is not None:
            message = user_message(text)
            self._append(message)
            self._active_request = text
        elif not self._deps.transcript.messages:
            yield AgentNotice("Nothing to continue.")
            return
        else:
            last = next(
                (
                    message
                    for message in reversed(self._deps.transcript.messages)
                    if message.role == "user" and message.text().strip()
                ),
                None,
            )
            if last is not None:
                self._active_request = last.text()

        recovery = _Recovery()
        turns = 0

        while turns < self._deps.max_turns:
            turns += 1
            prepared = await self._deps.context.prepare(
                self._deps.transcript,
                self._active_request,
                self._deps.profile,
                reactive=recovery.reactive_requested,
            )
            recovery.reactive_requested = False
            if prepared.checkpoint is not None:
                self._deps.session.save_context(prepared.checkpoint.to_dict())
            for event in prepared.events:
                yield AgentContextEvent(event.kind, event.detail)

            request = ModelRequest(
                model=self._deps.profile.model,
                system=self._deps.prompt_builder.build(self._prompt_context()),
                messages=prepared.messages,
                tools=self._deps.registry.specs(),
                max_output_tokens=self._deps.profile.max_output_tokens,
                profile=self._deps.profile,
                thinking=self._deps.thinking,
            )
            estimated = self._deps.estimator.estimate_messages(prepared.messages)

            assistant: Message | None = None
            stop_reason: StopReason = "end_turn"
            parse_errors: dict[str, str] = {}
            emitted_text = False
            emitted_partial = False
            try:
                async for provider_event in self._deps.provider.stream(request):
                    if isinstance(provider_event, TextDelta):
                        if provider_event.text:
                            emitted_text = True
                            emitted_partial = True
                            yield AgentTextDelta(provider_event.text)
                    elif isinstance(provider_event, ReasoningDelta):
                        if provider_event.text:
                            emitted_partial = True
                            yield AgentTextDelta(provider_event.text, reasoning=True)
                    elif isinstance(provider_event, ToolCall):
                        emitted_partial = True
                        if provider_event.parse_error:
                            parse_errors[provider_event.tool_use_id] = provider_event.parse_error
                        yield AgentToolStarted(
                            provider_event.tool_use_id,
                            provider_event.name,
                            provider_event.input,
                        )
                    elif isinstance(provider_event, ProviderError):
                        raise ProviderFailure(
                            provider_event.message,
                            provider_event.kind,
                            provider_event.status_code,
                        )
                    elif isinstance(provider_event, ResponseCompleted):
                        assistant = provider_event.message
                        if not emitted_text and assistant.text():
                            emitted_partial = True
                            yield AgentTextDelta(assistant.text())
                        self._append(assistant)
                        stop_reason = provider_event.stop_reason
                        self._usage = self._usage + provider_event.usage
                        self._deps.session.append_usage(
                            provider_event.usage, self._deps.profile.model
                        )
                        self._deps.estimator.calibrate(
                            estimated, provider_event.usage.input_tokens
                        )
                        yield AgentUsage(provider_event.usage, self._usage)
                        break
            except ProviderFailure as failure:
                if emitted_partial and failure.kind in ("retryable", "context_length"):
                    # Retrying would stream the same opening again. Stop and
                    # let the user decide whether to resend.
                    yield AgentError(
                        f"{failure.message} (after partial output; not retrying)",
                        failure.kind,
                        failure.status_code,
                    )
                    yield AgentTurnFinished("error", self._usage)
                    return
                action = await self._recover(failure, recovery)
                if action == "retry":
                    turns -= 1
                    continue
                if action == "reactive":
                    turns -= 1
                    continue
                yield AgentError(failure.message, failure.kind, failure.status_code)
                yield AgentTurnFinished("error", self._usage)
                return
            except asyncio.CancelledError:
                await self._after_turn("cancelled")
                raise

            if assistant is None:
                yield AgentError("provider stream ended without a final message", "fatal")
                yield AgentTurnFinished("error", self._usage)
                return

            recovery.retries = 0
            calls = assistant.tool_uses()
            if not calls:
                if stop_reason == "max_tokens" and recovery.continuations < MAX_CONTINUATIONS:
                    recovery.continuations += 1
                    self._append(user_message(CONTINUE_PROMPT))
                    yield AgentContextEvent("continue", "max output tokens; asking for continuation")
                    continue

                if stop_reason in ("refusal", "error"):
                    yield AgentError(
                        f"provider stopped with {stop_reason}", stop_reason
                    )
                elif stop_reason == "max_tokens":
                    yield AgentNotice(
                        "Output limit reached; the response may be incomplete."
                    )

                # Only a genuine end_turn may trigger memory extraction.
                await self._after_turn(stop_reason)
                yield AgentTurnFinished(stop_reason, self._usage)
                return

            results: list[ToolResultBlock] = []
            completed: set[str] = set()
            # The profile can change between calls (for example via /model), so
            # derive capability from the profile serving this request.
            tool_context = replace(
                self._deps.tool_context,
                supports_images=self._deps.profile.supports_images,
            )
            try:
                for call in calls:
                    result = await self._deps.executor.execute(
                        call,
                        ctx=tool_context,
                        policy=self._deps.policy,
                        asker=self._deps.asker,
                        parse_error=parse_errors.get(call.id),
                    )
                    completed.add(call.id)
                    results.append(_to_result_block(call.id, result))
                    yield AgentToolFinished(
                        call.id,
                        call.name,
                        result.content,
                        result.is_error,
                    )
            except asyncio.CancelledError:
                for call in calls:
                    if call.id not in completed:
                        results.append(_cancelled_result(call.id))
                self._append(tool_result_message(results))
                await self._after_turn("cancelled")
                raise

            self._append(tool_result_message(results))
            recovery.continuations = 0

        await self._after_turn("max_turns")
        yield AgentTurnFinished("max_turns", self._usage)

    async def compact(self) -> AsyncIterator[AgentEvent]:
        prepared = await self._deps.context.prepare(
            self._deps.transcript,
            self._active_request,
            self._deps.profile,
            force_compact=True,
        )
        if prepared.checkpoint is not None:
            self._deps.session.save_context(prepared.checkpoint.to_dict())
        for event in prepared.events:
            yield AgentContextEvent(event.kind, event.detail)
        if prepared.checkpoint is None and not any(
            event.kind == "compact_skipped" for event in prepared.events
        ):
            yield AgentNotice("Nothing to compact.")

    async def clear(self) -> None:
        # Persist first: a failed write must not leave memory ahead of disk.
        self._deps.session.append_clear()
        self._deps.session.save_context(None)
        self._deps.transcript.clear()
        self._deps.context.set_checkpoint(None)
        self._usage = Usage()
        self._active_request = ""

    async def switch_model(self, provider_name: str, model: str) -> None:
        if self._deps.provider_factory is None:
            raise RuntimeError("model switching is not configured")
        provider, profile, thinking = self._deps.provider_factory(provider_name, model)
        if thinking is not None and not profile.supports_thinking:
            await provider.aclose()
            raise ConfigError(
                f"provider {profile.provider_name!r} does not support extended thinking"
            )
        old = self._deps.provider
        self._deps.provider = provider
        self._deps.profile = profile
        self._deps.thinking = thinking
        if self._deps.provider_slot is not None:
            self._deps.provider_slot.replace(provider, profile, thinking)
        try:
            await old.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("closing previous provider failed", exc_info=True)
        self._deps.session.append_model(profile.provider_name, profile.model)

    async def close(self) -> None:
        task = self._extraction_task
        self._extraction_task = None
        if task is not None:
            if not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=15)
                except (TimeoutError, asyncio.CancelledError, Exception):
                    task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        try:
            await self._deps.provider.aclose()
        finally:
            self._deps.session.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _append(self, message: Message) -> None:
        self._deps.transcript.append(message)
        self._deps.session.append_message(message)

    def _prompt_context(self) -> PromptContext:
        memory = self._deps.memory
        return PromptContext(
            cwd=self._deps.tool_context.cwd,
            profile=self._deps.profile,
            tools=self._deps.registry.specs(),
            skills=self._deps.skills.catalog(),
            memory_catalog=tuple(memory.catalog_lines()) if memory else (),
            memories=tuple(memory.select(self._active_request)) if memory else (),
            project_instructions=self._deps.project_instructions,
            active_request=self._active_request,
            compaction_active=self._deps.context.checkpoint is not None,
        )

    async def _recover(self, failure: ProviderFailure, recovery: _Recovery) -> str:
        if failure.kind == "retryable" and recovery.retries < MAX_RETRIES:
            delay = 0.5 * (2**recovery.retries)
            recovery.retries += 1
            logger.info(
                "retryable provider error; retrying in %.1fs (attempt %d/%d)",
                delay,
                recovery.retries,
                MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            return "retry"
        if failure.kind == "context_length" and not recovery.attempted_reactive:
            recovery.attempted_reactive = True
            recovery.reactive_requested = True
            return "reactive"
        return "fail"

    async def _after_turn(self, stop_reason: str) -> None:
        if stop_reason != "end_turn":
            return
        if (
            not self._deps.memory_enabled
            or self._deps.memory is None
            or self._deps.extraction is None
            or self._deps.extractor is None
            or not self._deps.session.persist
        ):
            return
        if self._extraction_task is not None and not self._extraction_task.done():
            return
        self._extraction_task = asyncio.create_task(self._extract_memory())

    async def _extract_memory(self) -> None:
        try:
            await self._deps.extraction.request(  # type: ignore[union-attr]
                lambda: self._deps.transcript.messages,
                self._deps.extractor,  # type: ignore[arg-type]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory extraction task failed: %s", exc)


def _to_result_block(tool_use_id: str, result: ToolResult) -> ToolResultBlock:
    return ToolResultBlock(
        tool_use_id=tool_use_id,
        content=result.content,
        is_error=result.is_error,
    )


def _cancelled_result(tool_use_id: str) -> ToolResultBlock:
    return ToolResultBlock(
        tool_use_id=tool_use_id,
        content=(TextBlock(CANCELLED_RESULT),),
        is_error=True,
    )
