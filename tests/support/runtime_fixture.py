"""Build an AgentRuntime around a fake provider and tiny tools."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cli_agent.context.pipeline import ContextPipeline
from cli_agent.context.prompts import PromptBuilder
from cli_agent.context.skills import SkillLoader
from cli_agent.context.spill import SpillStore
from cli_agent.context.tokens import TokenEstimator
from cli_agent.messages import Usage
from cli_agent.permissions.policy import Asker, PermissionMode, PermissionPolicy
from cli_agent.providers.base import ModelProfile, ThinkingConfig
from cli_agent.runtime import AgentRuntime, RuntimeDeps
from cli_agent.session.paths import SessionPaths
from cli_agent.session.store import SessionStore
from cli_agent.tools.base import Tool, ToolContext, ToolResult, ToolSpec
from cli_agent.tools.execution import ToolExecutor
from cli_agent.tools.registry import ToolRegistry
from cli_agent.transcript import Transcript


class EchoTool:
    spec = ToolSpec(
        name="Echo",
        description="Echo text.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        category="read",
    )

    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append(dict(args))
        return ToolResult.text(str(args["text"]))


class ShellTool:
    spec = ToolSpec(
        name="Bash",
        description="Run a shell command.",
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
        category="shell",
    )

    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append(dict(args))
        return ToolResult.text("ran")


class FailTool:
    spec = ToolSpec(
        name="Fail",
        description="Always fails.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        category="read",
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        raise RuntimeError("boom")


async def fake_summarize(text: str, max_tokens: int) -> str:
    return f"SUMMARY({len(text)} chars)"


def make_runtime(
    provider: Any,
    *,
    tmp_path: Path,
    tools: list[Tool] | None = None,
    policy: PermissionPolicy | None = None,
    asker: Asker | None = None,
    max_turns: int = 50,
    checkpoint: Any = None,
    thinking: ThinkingConfig | None = None,
) -> tuple[AgentRuntime, SessionStore]:
    registry = ToolRegistry()
    for tool in tools or [EchoTool()]:
        registry.register(tool)
    executor = ToolExecutor(registry)
    permissions = policy or PermissionPolicy(
        PermissionMode.BYPASS, workspace=tmp_path
    )
    root = tmp_path / "session"
    paths = SessionPaths(
        root=root,
        jsonl=root / "session.jsonl",
        context=root / "context.json",
        meta=root / "meta.json",
        lock=root / "lock",
        spills=root / "spills",
        archive=root / "archive",
    )
    session = SessionStore(paths, persist=False)
    estimator = TokenEstimator()
    pipeline = ContextPipeline(
        estimator=estimator,
        spill=SpillStore(root / "spills"),
        session_paths=paths,
        summarize=fake_summarize,
        checkpoint=checkpoint,
    )
    profile = ModelProfile(
        provider_name="fake",
        provider_kind="anthropic_compat",
        model="fake-model",
        context_window=200_000,
        max_output_tokens=8_192,
    )
    deps = RuntimeDeps(
        provider=provider,
        profile=profile,
        transcript=Transcript(),
        registry=registry,
        executor=executor,
        policy=permissions,
        asker=asker,
        context=pipeline,
        prompt_builder=PromptBuilder(),
        session=session,
        tool_context=ToolContext(cwd=tmp_path, session_id="test", data_dir=tmp_path),
        skills=SkillLoader([]),
        project_instructions="",
        estimator=estimator,
        initial_usage=Usage(),
        max_turns=max_turns,
        memory=None,
        extraction=None,
        thinking=thinking,
    )
    return AgentRuntime(deps), session
