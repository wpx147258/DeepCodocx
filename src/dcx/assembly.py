"""The only concrete wiring point.

Everything else in the runtime depends on protocols or value types. If you need
to know which provider, store, or tool set is in use, look here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .atomic import atomic_write_text
from .config import AppConfig, load_config
from .context.memory import MemoryStore
from .context.pipeline import ContextCheckpoint, ContextPipeline
from .context.prompts import PromptBuilder, load_project_instructions
from .context.skills import SkillLoader
from .context.spill import SpillStore
from .context.tokens import TokenEstimator
from .deepdocx.library import DocumentLibrary, LibraryStore
from .deepdocx.profile import SessionProfile
from .errors import ConfigError, ProviderFailure
from .mcp.client import ServerConfig
from .mcp.tools import CallExternalTool, DiscoverTools, ExternalCatalog
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
from .session.attachments import AttachmentStore
from .session.checkpoints import CheckpointStore
from .session.fork import fork_session
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

    Summaries are best-effort background work. They do not share the main loop's
    recovery state, so a small local retry is enough.
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


_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _close_provider_best_effort(provider: Provider) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        with contextlib.suppress(Exception):
            asyncio.run(provider.aclose())
        return
    task = loop.create_task(provider.aclose())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_background_task_done)


def _background_task_done(task: asyncio.Task[Any]) -> None:
    _BACKGROUND_TASKS.discard(task)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.exception()


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
    fork: bool = False
    persist: bool = True
    max_turns: int | None = None
    memory_enabled: bool | None = None
    thinking_enabled: bool | None = None
    thinking_budget: int | None = None
    asker: Asker | None = None
    config_path: str | Path | None = None
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    mode: str | None = None
    library: str | None = None
    session_profile: SessionProfile | None = None


@dataclass(frozen=True, slots=True)
class RuntimeBundle:
    runtime: AgentRuntime
    config: AppConfig
    profile: ModelProfile
    session_id: str
    resumed: bool = False
    degraded: bool = False
    forked_from: str | None = None
    libraries: LibraryStore | None = None
    document_library: DocumentLibrary | None = None
    options: BuildOptions = field(default_factory=BuildOptions)


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
    if options.fork and not resumed:
        raise ConfigError("--fork requires --resume/-c")
    forked_from: str | None = None
    store: SessionStore | None = None
    checkpoint: ContextCheckpoint | None = None
    initial_usage = Usage()
    initial_mode: str | None = None
    initial_auto_previous: str | None = None
    degraded = False
    session_profile = options.session_profile or SessionProfile()
    libraries = LibraryStore(
        config.paths.data_dir,
        config.document_libraries,
        default_root=config.default_document_library,
        fallback_roots=tuple(str(base / "deep-docs-unified" / "data") for base in (cwd, *cwd.parents)[:4]),
    )
    if options.library:
        try:
            library_entry = libraries.register(options.library)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Cannot open document library: {exc}") from exc
        session_profile = SessionProfile(mode="deepdocx", library_id=library_entry["id"], library_root=library_entry["root"])
    elif options.mode:
        if options.mode not in ("normal", "deepdocx"):
            raise ConfigError("Unknown session mode")
        session_profile = replace(session_profile, mode=options.mode)
    transcript = Transcript()

    if resumed:
        session_id = options.resume_id or ""
        if session_id == "latest":
            latest = latest_session_id(config.paths, mode=session_profile.mode)
            if latest is None:
                raise ConfigError("no saved sessions are available to resume")
            session_id = latest
        if options.fork:
            # Fork first, then open the copy: the source may be open in another
            # process and is never touched.
            forked_from = session_id
            session_id = fork_session(config.paths, session_id)
        store, loaded = SessionStore.resume(
            config.paths, session_id, persist=options.persist
        )
        transcript = Transcript(loaded.messages)
        initial_usage = loaded.usage
        degraded = loaded.degraded
        if options.session_profile is None and options.library is None and options.mode is None or not options.fork and options.session_profile is None and options.library is None:
            session_profile = loaded.profile
        if options.fork and session_profile.mode == "deepdocx":
            session_profile = replace(session_profile, fork_boundary=len(loaded.messages), forked_from=forked_from)
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
        # The session's recorded mode beats the config default, but an explicit
        # --permission-mode wins over both.
        if options.permission_mode is None:
            initial_mode = loaded.permission_mode
            initial_auto_previous = loaded.auto_previous
    else:
        provider_name = config.default_provider
        model_name = config.default_model

    try:
        if session_profile.mode == "deepdocx":
            if not session_profile.library_root:
                entry = libraries.get(libraries.selected_id())
                session_profile = replace(session_profile, library_id=entry["id"], library_root=entry["root"])
            if not DocumentLibrary(Path(session_profile.library_root), session_profile.documents).documents():
                raise ValueError("Selected document library or document scope is empty")
        servers = _server_configs(config.mcp_servers)
        if session_profile.mode == "deepdocx":
            command = [sys.executable, "-m", "dcx.deepdocx.server", "--root", session_profile.library_root]
            semantic_settings=config.paths.data_dir/'semantic'/'settings'/f'{config.index.profile}-{config.caption.profile}.json'
            atomic_write_text(semantic_settings,json.dumps({'data_dir':str(config.paths.data_dir),'index':asdict(config.index),'caption':asdict(config.caption)}))
            command.extend(['--semantic-settings',str(semantic_settings),'--workspace',str(cwd)])
            for doc_id in session_profile.documents:
                command.extend(["--doc", doc_id])
            keys={key:env[key] for key in (config.index.api_key_env,config.index.joint_key_env,config.caption.api_key_env) if env.get(key)}
            servers.append(ServerConfig("documents", tuple(command), env=keys, read_tools=("doc_library", "doc_search", "doc_read", "doc_view", "doc_guide",'doc_semantic_search','doc_index_status'),timeout=120))
    except (ValueError, OSError) as exc:
        if store is not None:
            store.close()
        raise ConfigError(str(exc)) from exc

    provider = None
    try:
        provider_config = config.provider(provider_name)
        model_name = model_name or provider_config.default_model
        thinking_enabled = config.thinking_for(provider_name)
        thinking = ThinkingConfig(config.thinking_budget) if thinking_enabled else None
        provider, profile = build_provider(
            config,
            provider_name=provider_name,
            model=model_name,
            thinking_budget=config.thinking_budget if thinking_enabled else None,
            env=env,
        )
    except BaseException:
        if store is not None:
            with contextlib.suppress(Exception):
                store.close()
        raise

    slot = ProviderSlot(provider, profile, thinking)

    if store is None:
        try:
            store = SessionStore.new(
                config.paths,
                provider=profile.provider_name,
                model=profile.model,
                cwd=cwd,
                persist=options.persist,
            )
        except BaseException:
            _close_provider_best_effort(provider)
            raise

    try:
        attachments = AttachmentStore(store.paths.root)
        if session_profile.mode == "deepdocx":
            servers = [
                replace(server, command=(*server.command, "--session-root", str(store.paths.root)))
                if server.name == "documents" else server
                for server in servers
            ]
        memory_enabled = config.memory_enabled and options.persist
        skill_loader = SkillLoader.from_paths(
            config.paths.global_skills_dir,
            config.paths.project_skills_dir,
        )
        memory_store = MemoryStore(config.paths.memory_dir) if memory_enabled else None
        registry = build_builtin_registry(skill_loader, memory=memory_store)
        external = ExternalCatalog(servers)
        registry.register(DiscoverTools(external))
        registry.register(CallExternalTool())
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
        spill = SpillStore(store.spills_dir)
        executor = ToolExecutor(registry, spill=spill, external=external)
        checkpoints = (
            CheckpointStore(store.paths, config.paths.cwd)
            if options.persist
            else None
        )
        tool_context = ToolContext(
            cwd=config.paths.cwd,
            session_id=store.session_id,
            data_dir=config.paths.project_dir,
            checkpoints=checkpoints,
        )
        if not resumed or options.fork or session_profile != loaded.profile:
            store.append_profile(session_profile)
        estimator = TokenEstimator()

        complete = _make_completer(slot)

        async def summarize(text: str, max_tokens: int) -> str:
            prompt = _COMPACT_PROMPT + "\n\nConversation:\n" + text
            return await complete(prompt, max_tokens)

        pipeline = ContextPipeline(
            estimator=estimator,
            session_paths=store.paths,
            summarize=summarize,
            checkpoint=checkpoint,
            message_context=attachments.enrich if session_profile.mode == "deepdocx" else None,
        )
        prompt_builder = PromptBuilder()
        project_instructions = load_project_instructions(
            config.paths.cwd, config.paths.global_agents_md
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
            provider_factory=provider_factory,
            thinking=thinking,
            provider_slot=slot,
            checkpoints=checkpoints,
            session_profile=session_profile,
            external=external,
        )
        runtime = AgentRuntime(deps)
        runtime.restore_permission(initial_mode, initial_auto_previous)
        if session_profile.mode == "deepdocx":
            try:
                libraries.get(session_profile.library_id)
            except ValueError:
                libraries.register(session_profile.library_root)
            libraries.select(session_profile.library_id)
        return RuntimeBundle(
            runtime=runtime,
            config=config,
            profile=profile,
            session_id=store.session_id,
            resumed=resumed,
            degraded=degraded,
            forked_from=forked_from,
            libraries=libraries,
            document_library=DocumentLibrary(Path(session_profile.library_root), session_profile.documents) if session_profile.library_root else None,
            options=options,
        )

    except BaseException:
        _close_provider_best_effort(provider)
        store.close()
        raise


def _server_configs(raw: Mapping[str, Any]) -> list[ServerConfig]:
    configs = []
    for name, item in raw.items():
        if name == "documents" or not name or "." in name:
            raise ValueError("MCP server names must be nonempty without dots; documents is reserved")
        if not isinstance(item, dict) or not isinstance(item.get("command"), str):
            raise ValueError(f"mcp_servers.{name}.command must be an executable string")
        args = item.get("args", [])
        read_tools = item.get("read_tools", [])
        env = item.get("env", {})
        timeout = item.get("timeout", 30)
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError(f"mcp_servers.{name}.args must be a string array")
        if not isinstance(read_tools, list) or not all(isinstance(tool, str) for tool in read_tools):
            raise ValueError("read_tools must be a string array")
        if not isinstance(env, dict) or not all(isinstance(value, str) for value in env.values()):
            raise ValueError("MCP env must contain strings")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
            raise ValueError("MCP timeout must be between 0 and 600 seconds")
        configs.append(ServerConfig(name, (item["command"], *args), dict(env), tuple(read_tools), float(timeout)))
    return configs


_COMPACT_PROMPT = """You are a conversation summarizer. Summarize the conversation below into a
concise, factual reference that preserves:
1. Key decisions and outcomes.
2. Exact file paths, function names, identifiers, and changes.
3. Current task state and unresolved issues.
4. Commands or tests already run and their results.

Do not add opinions or instructions. Output only the summary text."""
