from __future__ import annotations

import asyncio
from pathlib import Path

from cli_agent.events import (
    AgentError,
    AgentRetry,
    AgentTextDelta,
    AgentToolFinished,
    AgentTurnFinished,
)
from cli_agent.permissions.policy import NullAsker, PermissionMode, PermissionPolicy
from cli_agent.providers.base import TextDelta
from tests.support.fake_provider import FakeProvider, fail, text_response, tool_response
from tests.support.runtime_fixture import EchoTool, ShellTool, make_runtime


async def test_text_only_turn(tmp_path: Path) -> None:
    provider = FakeProvider([[text_response("hello")]])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    events = [event async for event in runtime.run_turn("hi")]
    assert any(isinstance(event, AgentTextDelta) and event.text == "hello" for event in events)
    assert isinstance(events[-1], AgentTurnFinished)
    assert [message.role for message in runtime.messages] == ["user", "assistant"]
    assert len(provider.requests) == 1
    await runtime.close()
    assert provider.closed


async def test_tool_call_is_executed_and_result_fed_back(tmp_path: Path) -> None:
    echo = EchoTool()
    provider = FakeProvider(
        [
            [tool_response([("t1", "Echo", {"text": "hi"})])],
            [text_response("done")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[echo])
    events = [event async for event in runtime.run_turn("go")]
    assert echo.calls == [{"text": "hi"}]
    assert [message.role for message in runtime.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert runtime.messages[2].tool_results()[0].content[0].text == "hi"
    finished = next(event for event in events if isinstance(event, AgentToolFinished))
    assert not finished.is_error
    assert len(provider.requests) == 2
    await runtime.close()


async def test_unknown_tool_becomes_error_result(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            [tool_response([("t1", "Nope", {})])],
            [text_response("done")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[EchoTool()])
    await _consume(runtime, "go")
    result = runtime.messages[2].tool_results()[0]
    assert result.is_error
    assert "unknown tool" in result.content[0].text
    await runtime.close()


async def test_invalid_input_becomes_error_result(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            [tool_response([("t1", "Echo", {})])],
            [text_response("done")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[EchoTool()])
    await _consume(runtime, "go")
    result = runtime.messages[2].tool_results()[0]
    assert result.is_error
    assert "invalid arguments" in result.content[0].text
    await runtime.close()


async def test_permission_denial_becomes_error_result(tmp_path: Path) -> None:
    shell = ShellTool()
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    provider = FakeProvider(
        [
            [tool_response([("t1", "Bash", {"command": "echo hi"})])],
            [text_response("blocked")],
        ]
    )
    runtime, _ = make_runtime(
        provider, tmp_path=tmp_path, tools=[shell], policy=policy, asker=NullAsker()
    )
    await _consume(runtime, "run it")
    assert shell.calls == []
    result = runtime.messages[2].tool_results()[0]
    assert result.is_error
    assert "Permission denied" in result.content[0].text
    await runtime.close()


async def test_max_turns_stops_the_loop(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            [tool_response([("t1", "Echo", {"text": "hi"})])],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[EchoTool()], max_turns=1)
    events = [event async for event in runtime.run_turn("go")]
    assert isinstance(events[-1], AgentTurnFinished)
    assert events[-1].stop_reason == "max_turns"
    await runtime.close()


async def test_max_tokens_requests_continuation(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            [text_response("partial", stop_reason="max_tokens")],
            [text_response("done")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    await _consume(runtime, "go")
    assert len(provider.requests) == 2
    second = provider.requests[1]
    assert "Continue from where you left off" in second.messages[-1].text()
    await runtime.close()


async def test_retryable_error_is_retried(tmp_path: Path, monkeypatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    provider = FakeProvider([[fail("rate limited", "retryable")], [text_response("ok")]])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    events = [event async for event in runtime.run_turn("go")]
    assert len(provider.requests) == 2
    assert any(isinstance(event, AgentTextDelta) and event.text == "ok" for event in events)
    assert not any(isinstance(event, AgentError) for event in events)
    await runtime.close()


async def test_context_length_triggers_one_reactive_attempt(tmp_path: Path) -> None:
    provider = FakeProvider(
        [[fail("prompt too long", "context_length")], [text_response("ok")]]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    events = [event async for event in runtime.run_turn("go")]
    assert len(provider.requests) == 2
    assert not any(isinstance(event, AgentError) for event in events)
    assert any(isinstance(event, AgentTextDelta) and event.text == "ok" for event in events)
    await runtime.close()


async def test_fatal_error_is_reported(tmp_path: Path) -> None:
    provider = FakeProvider([[fail("boom", "fatal")]])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    events = [event async for event in runtime.run_turn("go")]
    assert any(isinstance(event, AgentError) for event in events)
    await runtime.close()


async def test_refusal_is_not_reported_as_end_turn(tmp_path: Path) -> None:
    provider = FakeProvider([[text_response("I can't help with that.", stop_reason="refusal")]])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    events = [event async for event in runtime.run_turn("do the thing")]
    finished = events[-1]
    assert isinstance(finished, AgentTurnFinished)
    assert finished.stop_reason == "refusal"
    await runtime.close()


async def test_retryable_error_emits_a_retry_notice(tmp_path: Path, monkeypatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    provider = FakeProvider([[fail("rate limited", "retryable")], [text_response("ok")]])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    events = [event async for event in runtime.run_turn("go")]
    notices = [event for event in events if isinstance(event, AgentRetry)]
    assert len(notices) == 1
    assert notices[0].attempt == 1
    assert notices[0].max_attempts == 3
    assert notices[0].reason == "rate limited"
    assert notices[0].partial is False
    await runtime.close()


async def test_retry_after_partial_output_is_marked_partial(
    tmp_path: Path, monkeypatch
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    provider = FakeProvider(
        [
            [TextDelta("half an answer "), fail("rate limited", "retryable")],
            [text_response("the whole answer")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    events = [event async for event in runtime.run_turn("go")]
    notices = [event for event in events if isinstance(event, AgentRetry)]
    assert len(notices) == 1
    assert notices[0].partial is True
    # The notice is emitted before the successful attempt streams.
    retry_index = events.index(notices[0])
    assert any(
        isinstance(event, AgentTextDelta) and event.text == "the whole answer"
        for event in events[retry_index:]
    )
    await runtime.close()


async def test_clear_resets_transcript(tmp_path: Path) -> None:
    provider = FakeProvider([[text_response("hello")]])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    await _consume(runtime, "hi")
    assert runtime.messages
    await runtime.clear()
    assert runtime.messages == ()
    await runtime.close()


async def _consume(runtime, text: str) -> None:
    async for _ in runtime.run_turn(text):
        pass


async def test_request_carries_thinking_config(tmp_path: Path) -> None:
    from cli_agent.providers.base import ThinkingConfig

    provider = FakeProvider([[text_response("ok")]])
    runtime, _ = make_runtime(
        provider, tmp_path=tmp_path, thinking=ThinkingConfig(2_048)
    )
    await _consume(runtime, "go")
    assert provider.requests[0].thinking == ThinkingConfig(2_048)
    assert runtime.thinking == ThinkingConfig(2_048)
    await runtime.close()
