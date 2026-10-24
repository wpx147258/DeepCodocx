"""The only concrete wiring point.

Everything else in the runtime depends on protocols or value types. If you need
to know which provider, store, or tool set is in use, look here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, load_config
from .context.memory import ExtractionCoordinator, MemoryExtractor, MemoryStore
from .context.pipeline import ContextCheckpoint, ContextPipeline
from .context.prompts import PromptBuilder, load_project_instructions
from .context.skills import SkillLoader
from .context.spill import SpillStore
from .context.tokens import TokenEstimator
from .errors import ConfigError, ProviderFailure
from .messages import Usage, user_message
from .permissions.policy import Asker, PermissionMode, PermissionPolicy
from .permissions.rules import PermissionRule, PermissionRules
from .providers.base import (
    ModelProfile,
    ModelRequest,
    Provider,
    ProviderError,
    ResponseCompleted,
    SystemSection,
    TextDelta,
    ThinkingConfig,
)
from .providers.factory import build_provider
from .runtime import AgentRuntime, ProviderSlot, RuntimeDeps
from .session.paths import latest_session_id
from .session.store import SessionStore
from .tools.base import ToolContext
from .tools.builtin import build_builtin_registry
from .tools.execution import ToolExecutor
from .transcript import Transcript

_SUMMARIZER_SYSTEM = (
    "You are a precise conversation summarizer. Follow the output format requested "
    "by the user and do not add opinions."
)
_HELPER_MAX_ATTEMPTS = 3
_HELPER_BASE_DELAY = 0.5
logger = logging.getLogger(__name__)


async def _complete_request(
    provider: Provider,
    request: ModelRequest,
    *,
    max_attempts: int = _HELPER_MAX_ATTEMPTS,
    base_delay: float = _HELPER_BASE_DELAY,
) -> str:
    """Run a tool-free helper request, retrying retryable provider failures.

    Summaries and memory extraction are best-effort background work. They do not
    share the main loop's recovery state, so a small local retry is enough.
    """
    last: ProviderFailure | None = None
    for attempt in range(max_attempts):
        parts: list[str] = []
        try:
            async for event in provider.stream(request):
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                elif isinstance(event, ProviderError):
                    raise ProviderFailure(event.message, event.kind, event.status_code)
                elif isinstance(event, ResponseCompleted) and not parts:
                    parts.append(event.message.text())
            return "".join(parts)
        except ProviderFailure as exc:
            last = exc
            if exc.kind != "retryable" or attempt >= max_attempts - 1:
                raise
            delay = base_delay * (2**attempt)
            logger.info(
                "helper provider call failed (%s); retrying in %.1fs", exc, delay
            )
            await asyncio.sleep(delay)
    if last is not None:
        raise last
    return ""


def _make_completer(slot: ProviderSlot) -> Callable[[str, int], Awaitable[str]]:
    """Create a tool-free helper caller bound to the current provider slot."""

    async def complete(prompt: str, max_tokens: int) -> str:
        profile = slot.profile
        request = ModelRequest(
            model=profile.model,
            system=(SystemSection("summarizer", _SUMMARIZER_SYSTEM),),
            messages=(user_message(prompt),),
            tools=(),
            max_output_tokens=max_tokens,
            profile=profile,
        )
        return await _complete_request(slot.provider, request)

    return complete


@dataclass(frozen=True, slots=True)
class BuildOptions:
    provider: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    resume_id: str | None = None
    persist: bool = True
    max_turns: int | None = None
    memory_enabled: bool | None = None
    thinking_enabled: bool | None = None
    thinking_budget: int | None = None
    asker: Asker | None = None
    config_path: str | Path | None = None
    cwd: Path | None = None
    env: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeBundle:
    runtime: AgentRuntime
    config: AppConfig
    profile: ModelProfile
    session_id: str
    resumed: bool = False
    degraded: bool = False


def build_runtime(options: BuildOptions | None = None) -> RuntimeBundle:
    options = options or BuildOptions()
    env = options.env if options.env is not None else os.environ
    cwd = (options.cwd or Path.cwd()).resolve()
    config = load_config(options.config_path, cwd=cwd, env=env)
    config = config.with_overrides(
        provider=options.provider,
        model=options.model,
        permission_mode=options.permission_mode,
        max_turns=options.max_turns,
        memory_enabled=options.memory_enabled,
        thinking_enabled=options.thinking_enabled,
        thinking_budget=options.thinking_budget,
    )

    resumed = options.resume_id is not None
    store: SessionStore | None = None
    checkpoint: ContextCheckpoint | None = None
    initial_usage = Usage()
    degraded = False
    transcript = Transcript()

    if resumed:
        session_id = options.resume_id or ""
        if session_id == "latest":
            latest = latest_session_id(config.paths)
            if latest is None:
                raise ConfigError("no saved sessions are available to resume")
            session_id = latest
        store, loaded = SessionStore.resume(
            config.paths, session_id, persist=options.persist
        )
        transcript = Transcript(loaded.messages)
        initial_usage = loaded.usage
        degraded = loaded.degraded
        # Explicit CLI choices win over the session record. If the provider
        # changes without an explicit model, do not carry the old session's
        # model across providers; let the new provider default apply.
        provider_name = options.provider or loaded.provider or config.default_provider
        if options.model:
            model_name = options.model
        elif options.provider and options.provider != loaded.provider:
            # A provider change must not carry the old provider's model across.
            model_name = None
        else:
            model_name = loaded.model or config.default_model
        if loaded.checkpoint:
            checkpoint = ContextCheckpoint.from_dict(loaded.checkpoint)
    else:
        provider_name = config.default_provider
        model_name = config.default_model

    provider_config = config.provider(provider_name)
    model_name = model_name or provider_config.default_model

    thinking_enabled = config.thinking_for(provider_name)
    thinking = ThinkingConfig(config.thinking_budget) if thinking_enabled else None
    try:
        provider, profile = build_provider(
            config,
            provider_name=provider_name,
            model=model_name,
            thinking_budget=config.thinking_budget if thinking_enabled else None,
            env=env,
        )
    except Exception:
        if store is not None:
            store.close()
        raise

    slot = ProviderSlot(provider, profile, thinking)

    if store is None:
        store = SessionStore.new(
            config.paths,
            provider=profile.provider_name,
            model=profile.model,
            cwd=cwd,
            persist=options.persist,
        )

    memory_enabled = config.memory_enabled and options.persist
    skill_loader = SkillLoader.from_paths(
        config.paths.global_skills_dir,
        config.paths.project_skills_dir,
    )
    memory_store = MemoryStore(config.paths.memory_dir) if memory_enabled else None
    registry = build_builtin_registry(skill_loader)
    rules = PermissionRules(
        allow=tuple(
            PermissionRule(rule.tool, dict(rule.args)) for rule in config.allow_rules
        ),
        deny=tuple(
            PermissionRule(rule.tool, dict(rule.args)) for rule in config.deny_rules
        ),
    )
    policy = PermissionPolicy(
        PermissionMode(config.permission_mode),
        rules=rules,
        workspace=config.paths.cwd,
    )
    executor = ToolExecutor(registry)
    tool_context = ToolContext(
        cwd=config.paths.cwd,
        session_id=store.session_id,
        data_dir=config.paths.project_dir,
    )
    estimator = TokenEstimator()
    spill = SpillStore(store.spills_dir)

    complete = _make_completer(slot)

    async def summarize(text: str, max_tokens: int) -> str:
        prompt = _COMPACT_PROMPT + "\n\nConversation:\n" + text
        return await complete(prompt, max_tokens)

    pipeline = ContextPipeline(
        estimator=estimator,
        spill=spill,
        session_paths=store.paths,
        summarize=summarize,
        checkpoint=checkpoint,
    )
    prompt_builder = PromptBuilder()
    project_instructions = load_project_instructions(
        config.paths.cwd, config.paths.global_agents_md
    )

    extractor: MemoryExtractor | None = None
    coordinator: ExtractionCoordinator | None = None
    if memory_store is not None:
        async def extract_complete(prompt: str) -> str:
            return await complete(prompt, 2_000)

        extractor = MemoryExtractor(memory_store, extract_complete)
        coordinator = ExtractionCoordinator(
            min_new_messages=config.memory_extraction_min_new_messages,
            cooldown_seconds=config.memory_extraction_cooldown_seconds,
        )

    def provider_factory(
        provider_name: str, model: str
    ) -> tuple[Provider, ModelProfile, ThinkingConfig | None]:
        enabled = config.thinking_for(provider_name)
        provider, profile = build_provider(
            config,
            provider_name=provider_name,
            model=model,
            thinking_budget=config.thinking_budget if enabled else None,
            env=env,
        )
        thinking = ThinkingConfig(config.thinking_budget) if enabled else None
        return provider, profile, thinking

    deps = RuntimeDeps(
        provider=provider,
        profile=profile,
        transcript=transcript,
        registry=registry,
        executor=executor,
        policy=policy,
        asker=options.asker,
        context=pipeline,
        prompt_builder=prompt_builder,
        session=store,
        tool_context=tool_context,
        skills=skill_loader,
        project_instructions=project_instructions,
        estimator=estimator,
        initial_usage=initial_usage,
        max_turns=config.max_turns,
        memory=memory_store,
        memory_enabled=memory_enabled,
        extraction=coordinator,
        extractor=extractor,
        provider_factory=provider_factory,
        thinking=thinking,
        provider_slot=slot,
    )
    runtime = AgentRuntime(deps)
    return RuntimeBundle(
        runtime=runtime,
        config=config,
        profile=profile,
        session_id=store.session_id,
        resumed=resumed,
        degraded=degraded,
    )


_COMPACT_PROMPT = """You are a conversation summarizer. Summarize the conversation below into a
concise, factual reference that preserves:
1. Key decisions and outcomes.
2. Exact file paths, function names, identifiers, and changes.
3. Current task state and unresolved issues.
4. Commands or tests already run and their results.

Do not add opinions or instructions. Output only the summary text."""
