"""The one agent loop.

``AgentRuntime`` owns the canonical transcript, the active provider/profile, and
cumulative usage. Hosts only submit user text and consume events.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field

from .context.memory import MemoryRecord, MemoryStore, inject_memory, memory_note
from .context.pipeline import ContextPipeline
from .context.prompts import PromptBuilder, PromptContext
from .context.skills import SkillLoader
from .context.tokens import TokenEstimator, prompt_tokens
from .errors import ConfigError, ProviderFailure
from .events import (
    AgentContextEvent,
    AgentError,
    AgentEvent,
    AgentNotice,
    AgentRetry,
    AgentToolFinished,
    AgentTurnFinished,
    AgentUsage,
)
from .messages import (
    ImageBlock,
    Message,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    tool_result_message,
    user_message,
)
from .permissions.controller import PermissionController
from .permissions.policy import Asker, PermissionPolicy
from .providers.base import (
    ModelProfile,
    ModelRequest,
    Provider,
    ThinkingConfig,
)
from .runtime_attempt import Attempt, stream_attempt
from .session.checkpoints import CheckpointPreview, CheckpointStore, RestoreResult
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
    provider_factory: Callable[[str, str], ProviderBuild] | None = None
    thinking: ThinkingConfig | None = None
    provider_slot: ProviderSlot | None = None
    checkpoints: CheckpointStore | None = None


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
        self._last_request_tokens = 0
        self._last_usage = Usage()
        # Providers replaced by /model. They stay open until shutdown because a
        # background summary call may still hold one.
        self._retired: list[Provider] = []
        self._permissions = PermissionController(
            deps.policy,
            on_change=lambda mode, previous: deps.session.append_permission_mode(
                mode, previous
            ),
        )

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
    def supports_images(self) -> bool:
        return self._deps.profile.supports_images

    @property
    def provider_name(self) -> str:
        return self._deps.profile.provider_name

    @property
    def thinking(self) -> ThinkingConfig | None:
        return self._deps.thinking

    @property
    def context_tokens(self) -> int:
        """Estimated size of the current view (messages + system + tools).

        A resumed or freshly opened session has not issued a request yet; fall
        back to an estimate so the context meter does not read zero.
        """
        if self._last_request_tokens > 0:
            return self._last_request_tokens
        try:
            _, estimated = self._build_request(
                self._deps.transcript.for_request()
            )
        except Exception:  # noqa: BLE001 - a readout must never break the host
            logger.debug("could not estimate context size", exc_info=True)
            return 0
        return estimated

    @property
    def context_window(self) -> int:
        return self._deps.profile.context_window

    @property
    def last_usage(self) -> Usage:
        """Usage reported for the most recent provider request."""
        return self._last_usage

    @property
    def permission_mode(self) -> str:
        return self._permissions.mode

    def set_permission_mode(self, mode: str, *, persist: bool = True) -> str:
        return self._permissions.set_mode(mode, persist=persist)

    def set_auto(self, enabled: bool, *, persist: bool = True) -> str:
        return self._permissions.set_auto(enabled, persist=persist)

    def toggle_auto(self, *, persist: bool = True) -> str:
        return self._permissions.toggle_auto(persist=persist)

    def restore_permission(self, mode: str | None, auto_previous: str | None = None) -> None:
        self._permissions.restore(mode, auto_previous)

    def memories(self) -> tuple[MemoryRecord, ...]:
        if self._deps.memory is None:
            return ()
        return tuple(self._deps.memory.all())

    def forget_memory(self, name: str) -> bool:
        if self._deps.memory is None:
            return False
        return self._deps.memory.forget(name)

    async def run_turn(
        self,
        text: str | None = None,
        *,
        images: Sequence[ImageBlock] = (),
    ) -> AsyncIterator[AgentEvent]:
        turn_id: str | None = None
        if text is not None or images:
            message = user_message(text or "", images=images)
            self._append(message)
            self._active_request = text or ""
            turn_id = message.id
        elif not self._deps.transcript.messages:
            yield AgentNotice("Nothing to continue.")
            return
        else:
            last = self._last_user_message()
            if last is not None:
                self._active_request = last.text()
                turn_id = last.id

        if self._deps.checkpoints is not None and turn_id is not None:
            self._deps.checkpoints.begin(turn_id)

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
            for context_event in prepared.events:
                yield AgentContextEvent(context_event.kind, context_event.detail)

            request, estimated = self._build_request(prepared.messages)
            self._last_request_tokens = estimated

            attempt = Attempt()
            try:
                async for agent_event in stream_attempt(
                    self._deps.provider, request, attempt
                ):
                    yield agent_event
            except ProviderFailure as failure:
                action, delay = await self._recover(failure, recovery)
                if action == "retry":
                    yield AgentRetry(
                        attempt=recovery.retries,
                        max_attempts=MAX_RETRIES,
                        kind=failure.kind,
                        reason=failure.message,
                        partial=attempt.emitted_text,
                    )
                    await asyncio.sleep(delay)
                    turns -= 1
                    continue
                if action == "reactive":
                    turns -= 1
                    continue
                yield AgentError(failure.message, failure.kind, failure.status_code)
                yield AgentTurnFinished("error", self._usage)
                return
            except asyncio.CancelledError:
                raise

            if attempt.assistant is None:
                yield AgentError("provider stream ended without a final message", "fatal")
                yield AgentTurnFinished("error", self._usage)
                return

            self._append(attempt.assistant)
            self._last_usage = attempt.usage
            self._usage = self._usage + attempt.usage
            self._deps.session.append_usage(attempt.usage, self._deps.profile.model)
            yield AgentUsage(attempt.usage, self._usage)
            self._deps.estimator.calibrate(estimated, prompt_tokens(attempt.usage))
            recovery.retries = 0

            calls = attempt.assistant.tool_uses()
            if not calls:
                events, finish = self._finish_attempt(attempt, recovery)
                for pending_event in events:
                    yield pending_event
                if finish is None:
                    continue
                if finish in ("refusal", "error"):
                    yield AgentError(f"provider stopped with {finish}", finish)
                elif finish == "max_tokens":
                    yield AgentNotice(
                        "Output limit reached; the response may be incomplete."
                    )
                yield AgentTurnFinished(finish, self._usage)
                return

            async for tool_event in self._run_tool_calls(calls, attempt.parse_errors):
                yield tool_event
            recovery.continuations = 0

        yield AgentTurnFinished("max_turns", self._usage)

    async def rewind_to(self, message_id: str) -> None:
        """Drop a user message and everything after it (for edit-and-resend)."""
        keep, _ = self._rewind_target(message_id=message_id)
        self._rewind(keep)

    async def rewind_last_user_turn(self) -> None:
        """Keep the latest user turn and drop the assistant/tool tail."""
        keep, _ = self._rewind_target(last_user=True)
        self._rewind(keep)

    def preview_checkpoints(
        self, *, message_id: str | None = None, last_user: bool = False
    ) -> CheckpointPreview:
        if self._deps.checkpoints is None:
            return CheckpointPreview()
        _, turn_ids = self._rewind_target(
            message_id=message_id, last_user=last_user
        )
        return self._deps.checkpoints.preview(turn_ids)

    def restore_checkpoints(
        self,
        *,
        message_id: str | None = None,
        last_user: bool = False,
        force: bool = False,
    ) -> RestoreResult:
        if self._deps.checkpoints is None:
            return RestoreResult()
        _, turn_ids = self._rewind_target(
            message_id=message_id, last_user=last_user
        )
        return self._deps.checkpoints.restore(turn_ids, force=force)

    def _rewind_target(
        self, *, message_id: str | None = None, last_user: bool = False
    ) -> tuple[int, list[str]]:
        messages = self._deps.transcript.messages
        if last_user:
            index = next(
                (
                    position
                    for position in range(len(messages) - 1, -1, -1)
                    if messages[position].role == "user"
                    and _has_user_content(messages[position])
                ),
                None,
            )
            if index is None:
                raise ValueError("no user message to regenerate")
            return index + 1, [messages[index].id]
        if not message_id:
            raise ValueError("message_id is required")
        index = self._deps.transcript.index_of(message_id)
        if index is None:
            raise ValueError("message not found")
        message = messages[index]
        if message.role != "user":
            raise ValueError("only user messages can be edited")
        turn_ids = [item.id for item in messages[index:] if item.role == "user"]
        return index, turn_ids

    def _last_user_message(self) -> Message | None:
        return next(
            (
                message
                for message in reversed(self._deps.transcript.messages)
                if message.role == "user" and _has_user_content(message)
            ),
            None,
        )

    def _rewind(self, keep: int) -> None:
        messages = self._deps.transcript.messages
        keep = max(0, min(keep, len(messages)))
        self._deps.transcript.truncate(keep)
        self._deps.session.append_rewind(keep)
        self._deps.context.set_checkpoint(None)
        self._deps.session.save_context(None)
        self._last_request_tokens = 0
        self._last_usage = Usage()
        previous = next(
            (
                message.text()
                for message in reversed(messages[:keep])
                if message.role == "user" and message.text().strip()
            ),
            "",
        )
        self._active_request = previous

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
        if prepared.checkpoint is None:
            yield AgentNotice("Nothing to compact.")

    async def clear(self) -> None:
        self._deps.transcript.clear()
        self._deps.context.set_checkpoint(None)
        self._deps.session.append_clear()
        self._deps.session.save_context(None)
        self._active_request = ""
        self._last_request_tokens = 0
        self._last_usage = Usage()

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
        # Let the context meter recompute against the new window/system prompt.
        self._last_request_tokens = 0
        if self._deps.provider_slot is not None:
            self._deps.provider_slot.replace(provider, profile, thinking)
        if old is not provider:
            # Background helpers may still be mid-call on the old provider, and
            # summarization would otherwise keep using a closed client. Close it
            # at shutdown instead.
            self._retired.append(old)
        self._deps.session.append_model(profile.provider_name, profile.model)

    async def close(self) -> None:
        retired, self._retired = self._retired, []
        try:
            await self._deps.provider.aclose()
        finally:
            for provider in retired:
                with contextlib.suppress(Exception):
                    await provider.aclose()
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
            project_instructions=self._deps.project_instructions,
            active_request=self._active_request,
            compaction_active=self._deps.context.checkpoint is not None,
            memory_enabled=memory is not None,
        )

    def _build_request(self, messages: Sequence[Message]) -> tuple[ModelRequest, int]:
        system = self._deps.prompt_builder.build(self._prompt_context())
        tools = self._deps.registry.specs()
        # Memory rides on the newest user message so the system prompt (and the
        # cacheable prefix) stays stable when a record is saved.
        view = inject_memory(
            messages, memory_note(self._deps.memory, self._active_request)
        )
        request = ModelRequest(
            model=self._deps.profile.model,
            system=system,
            messages=view,
            tools=tools,
            max_output_tokens=self._deps.profile.max_output_tokens,
            profile=self._deps.profile,
            thinking=self._deps.thinking,
        )
        # Tools and the system prompt consume real input tokens; leaving them out
        # made the calibration ratio drift toward the clamp.
        estimated = self._deps.estimator.estimate_request(view, system, tools)
        return request, estimated

    def _finish_attempt(
        self, attempt: Attempt, recovery: _Recovery
    ) -> tuple[list[AgentEvent], StopReason | None]:
        """What a completed, tool-free attempt means: ask for more, or finish."""
        if (
            attempt.stop_reason == "max_tokens"
            and recovery.continuations < MAX_CONTINUATIONS
        ):
            recovery.continuations += 1
            self._append(user_message(CONTINUE_PROMPT))
            return (
                [AgentContextEvent("continue", "max output tokens; asking for continuation")],
                None,
            )
        # A refusal or a truncated turn is not a successful end_turn, and hosts
        # exit non-zero for it.
        reason = attempt.stop_reason
        final_reason: StopReason = "error" if reason == "tool_use" else reason
        return [], final_reason

    async def _run_tool_calls(
        self, calls: Sequence[ToolUseBlock], parse_errors: dict[str, str]
    ) -> AsyncIterator[AgentToolFinished]:
        """Authorize every call in order, then execute in concurrency-safe batches."""
        results: list[ToolResultBlock] = []
        completed: set[str] = set()
        try:
            # Authorization can wait on a user prompt; cancellation must close
            # the tool_use/tool_result pair even before any tool starts.
            prepared = [
                await self._deps.executor.prepare(
                    call,
                    policy=self._deps.policy,
                    asker=self._deps.asker,
                    parse_error=parse_errors.get(call.id),
                )
                for call in calls
            ]
            async for item, result in self._deps.executor.run_all(
                prepared, ctx=self._deps.tool_context
            ):
                completed.add(item.call.id)
                results.append(_to_result_block(item.call.id, result))
                yield AgentToolFinished(
                    item.call.id,
                    item.call.name,
                    result.content,
                    result.is_error,
                )
        except asyncio.CancelledError:
            for call in calls:
                if call.id not in completed:
                    results.append(_cancelled_result(call.id))
            self._append(tool_result_message(results))
            raise

        self._append(tool_result_message(results))

    async def _recover(self, failure: ProviderFailure, recovery: _Recovery) -> tuple[str, float]:
        """Decide what to do with a failed attempt, and how long to wait.

        The caller emits the retry notice and sleeps, so the delay is part of the
        decision rather than a side effect here.
        """
        if failure.kind == "retryable" and recovery.retries < MAX_RETRIES:
            delay = 0.5 * (2**recovery.retries)
            recovery.retries += 1
            logger.info(
                "retryable provider error; retrying in %.1fs (attempt %d/%d)",
                delay,
                recovery.retries,
                MAX_RETRIES,
            )
            return "retry", delay
        if failure.kind == "context_length" and not recovery.attempted_reactive:
            recovery.attempted_reactive = True
            recovery.reactive_requested = True
            return "reactive", 0.0
        return "fail", 0.0


def _has_user_content(message: Message) -> bool:
    return any(
        isinstance(block, (TextBlock, ImageBlock)) for block in message.content
    )


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
