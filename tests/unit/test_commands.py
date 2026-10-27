from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from cli_agent.cli.commands import CommandDispatcher
from cli_agent.cli.render import Renderer
from cli_agent.messages import Usage
from cli_agent.permissions.policy import NullAsker, PermissionMode, PermissionPolicy
from tests.support.fake_provider import FakeProvider, text_response, tool_response
from tests.support.runtime_fixture import ShellTool, make_runtime


def _renderer() -> tuple[Renderer, StringIO]:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)
    return Renderer(console=console), output


async def test_auto_toggles_full_permission_mode(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    runtime, _ = make_runtime(FakeProvider([]), tmp_path=tmp_path, policy=policy)
    renderer, output = _renderer()
    dispatcher = CommandDispatcher()
    assert runtime.permission_mode == "default"

    result = await dispatcher.dispatch("/auto", runtime, renderer)
    assert result.handled
    assert runtime.permission_mode == "bypass"
    assert "without asking" in output.getvalue()

    await dispatcher.dispatch("/auto", runtime, renderer)
    assert runtime.permission_mode == "default"
    await runtime.close()


async def test_auto_on_and_off_are_explicit(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    runtime, _ = make_runtime(FakeProvider([]), tmp_path=tmp_path, policy=policy)
    renderer, _ = _renderer()
    dispatcher = CommandDispatcher()

    await dispatcher.dispatch("/auto on", runtime, renderer)
    assert runtime.permission_mode == "bypass"
    await dispatcher.dispatch("/auto off", runtime, renderer)
    assert runtime.permission_mode == "default"
    await runtime.close()


async def test_auto_rejects_unknown_argument(tmp_path: Path, capsys) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    runtime, _ = make_runtime(FakeProvider([]), tmp_path=tmp_path, policy=policy)
    renderer, _ = _renderer()

    await CommandDispatcher().dispatch("/auto maybe", runtime, renderer)
    assert runtime.permission_mode == "default"
    assert "usage: /auto" in capsys.readouterr().err
    await runtime.close()


async def test_auto_mode_executes_tools_without_asking(tmp_path: Path) -> None:
    shell = ShellTool()
    provider = FakeProvider(
        [
            [tool_response([("t1", "Bash", {"command": "echo hi"})])],
            [text_response("done")],
        ]
    )
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    runtime, _ = make_runtime(
        provider,
        tmp_path=tmp_path,
        tools=[shell],
        policy=policy,
        asker=NullAsker(),
    )
    renderer, _ = _renderer()
    await CommandDispatcher().dispatch("/auto", runtime, renderer)

    async for _ in runtime.run_turn("run it"):
        pass
    assert shell.calls == [{"command": "echo hi"}]
    await runtime.close()


async def test_help_lists_auto(tmp_path: Path) -> None:
    runtime, _ = make_runtime(FakeProvider([]), tmp_path=tmp_path)
    renderer, output = _renderer()
    await CommandDispatcher().dispatch("/help", runtime, renderer)
    assert "/auto" in output.getvalue()
    await runtime.close()


async def test_cost_shows_context_usage(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            [
                text_response(
                    "ok",
                    usage=Usage(
                        input_tokens=100,
                        output_tokens=20,
                        cache_read_tokens=900,
                    ),
                )
            ]
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    renderer, output = _renderer()
    dispatcher = CommandDispatcher()

    async for _ in runtime.run_turn("hi"):
        pass
    await dispatcher.dispatch("/cost", runtime, renderer)

    text = output.getvalue()
    assert "prompt 1000 sent" in text
    assert "uncached 100" in text
    assert "cache read 900" in text
    assert "out 20" in text
    assert "context: ~" in text
    assert "200000" in text
    assert "cache: last 90.0% hit" in text
    assert "session 90.0% hit" in text
    await runtime.close()


async def test_compact_warns_about_the_prefix_rewrite(tmp_path: Path) -> None:
    runtime, _ = make_runtime(FakeProvider([]), tmp_path=tmp_path)
    renderer, output = _renderer()

    await CommandDispatcher().dispatch("/compact", runtime, renderer)
    assert "rewrites the prompt prefix" in output.getvalue()
    await runtime.close()


async def test_memory_command_lists_and_forgets(tmp_path: Path) -> None:
    from cli_agent.context.memory import MemoryRecord, MemoryStore

    store = MemoryStore(tmp_path / "memory")
    store.save(MemoryRecord("prefers-tabs", "indent preference", "user", "Use tabs."))
    runtime, _ = make_runtime(FakeProvider([]), tmp_path=tmp_path, memory=store)
    renderer, output = _renderer()
    dispatcher = CommandDispatcher()

    await dispatcher.dispatch("/memory", runtime, renderer)
    assert "prefers-tabs" in output.getvalue()

    await dispatcher.dispatch("/memory forget prefers-tabs", runtime, renderer)
    assert store.all() == []
    await dispatcher.dispatch("/memory", runtime, renderer)
    assert "No saved memories." in output.getvalue()
    await runtime.close()


async def test_memory_command_without_a_store(tmp_path: Path) -> None:
    runtime, _ = make_runtime(FakeProvider([]), tmp_path=tmp_path)
    renderer, output = _renderer()
    dispatcher = CommandDispatcher()

    await dispatcher.dispatch("/memory", runtime, renderer)
    assert "No saved memories." in output.getvalue()
    await dispatcher.dispatch("/memory forget anything", runtime, renderer)
    assert "No memory named" in output.getvalue()
    await runtime.close()


async def test_help_lists_memory(tmp_path: Path) -> None:
    runtime, _ = make_runtime(FakeProvider([]), tmp_path=tmp_path)
    renderer, output = _renderer()
    await CommandDispatcher().dispatch("/help", runtime, renderer)
    text = output.getvalue()
    assert "/memory" in text
    await runtime.close()
