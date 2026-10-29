from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from dcx.context.tokens import TokenEstimator
from dcx.events import (
    AgentError,
    AgentNotice,
    AgentRetry,
    AgentTextDelta,
    AgentToolFinished,
    AgentTurnFinished,
)
from dcx.messages import ImageBlock, Usage
from dcx.permissions.policy import NullAsker, PermissionMode, PermissionPolicy
from dcx.providers.base import TextDelta
from dcx.tools.base import ToolContext, ToolResult, ToolSpec
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


async def test_high_risk_shell_command_is_blocked_without_an_asker(tmp_path: Path) -> None:
    shell = ShellTool()
    policy = PermissionPolicy(PermissionMode.BYPASS, workspace=tmp_path)
    provider = FakeProvider(
        [
            [tool_response([("t1", "Bash", {"command": "rm -rf /"})])],
            [text_response("giving up")],
        ]
    )
    runtime, _ = make_runtime(
        provider,
        tmp_path=tmp_path,
        tools=[shell],
        policy=policy,
        asker=NullAsker(),
    )
    await _consume(runtime, "clean the disk")

    assert shell.calls == []
    result = runtime.messages[2].tool_results()[0]
    assert result.is_error
    assert "high-risk" in result.content[0].text
    await runtime.close()


async def test_request_carries_thinking_config(tmp_path: Path) -> None:
    from dcx.providers.base import ThinkingConfig

    provider = FakeProvider([[text_response("ok")]])
    runtime, _ = make_runtime(
        provider, tmp_path=tmp_path, thinking=ThinkingConfig(2_048)
    )
    await _consume(runtime, "go")
    assert provider.requests[0].thinking == ThinkingConfig(2_048)
    assert runtime.thinking == ThinkingConfig(2_048)
    await runtime.close()


class _BarrierReadTool:
    """Only completes if two calls are in flight at the same time."""

    spec = ToolSpec(
        name="Read",
        description="Read a file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        category="read",
        concurrency_safe=True,
    )

    def __init__(self, barrier: asyncio.Barrier) -> None:
        self.barrier = barrier
        self.paths: list[str] = []

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        self.paths.append(str(args["path"]))
        await asyncio.wait_for(self.barrier.wait(), timeout=5)
        return ToolResult.text(f"read {args['path']}")


class _RecordingTool:
    def __init__(self, name: str, log: list[str], *, safe: bool, category: str) -> None:
        self.log = log
        self.spec = ToolSpec(
            name=name,
            description=name,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            category=category,  # type: ignore[arg-type]
            concurrency_safe=safe,
        )

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        self.log.append(self.spec.name)
        await asyncio.sleep(0)
        return ToolResult.text(self.spec.name)


async def test_concurrency_safe_calls_run_in_parallel(tmp_path: Path) -> None:
    tool = _BarrierReadTool(asyncio.Barrier(2))
    provider = FakeProvider(
        [
            [tool_response([("t1", "Read", {"path": "a"}), ("t2", "Read", {"path": "b"})])],
            [text_response("done")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[tool])
    events = [event async for event in runtime.run_turn("go")]
    finished = [event for event in events if isinstance(event, AgentToolFinished)]
    # Results keep the model's call order even though the runs overlap.
    assert [event.tool_use_id for event in finished] == ["t1", "t2"]
    assert not any(event.is_error for event in finished)
    assert tool.paths == ["a", "b"]
    await runtime.close()


async def test_unsafe_calls_act_as_parallel_barriers(tmp_path: Path) -> None:
    log: list[str] = []
    read = _RecordingTool("Read", log, safe=True, category="read")
    write = _RecordingTool("Write", log, safe=False, category="write")
    provider = FakeProvider(
        [
            [
                tool_response(
                    [
                        ("t1", "Read", {}),
                        ("t2", "Write", {}),
                        ("t3", "Read", {}),
                    ]
                )
            ],
            [text_response("done")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[read, write])
    await _consume(runtime, "go")
    assert log == ["Read", "Write", "Read"]
    await runtime.close()


async def test_cancelling_parallel_tools_records_cancelled_results(tmp_path: Path) -> None:
    started = asyncio.Event()

    class _BlockingTool:
        spec = ToolSpec(
            name="Read",
            description="Read a file.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            category="read",
            concurrency_safe=True,
        )

        async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
            started.set()
            await asyncio.Event().wait()
            return ToolResult.text("never")

    provider = FakeProvider(
        [
            [tool_response([("t1", "Read", {"path": "a"}), ("t2", "Read", {"path": "b"})])],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[_BlockingTool()])

    async def consume() -> None:
        async for _ in runtime.run_turn("go"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    results = runtime.messages[-1].tool_results()
    assert {result.tool_use_id for result in results} == {"t1", "t2"}
    assert all(result.is_error for result in results)
    assert "cancelled" in results[0].content[0].text.lower()
    await runtime.close()


async def test_calibration_counts_cached_prompt_tokens(tmp_path: Path) -> None:
    estimator = TokenEstimator()
    provider = FakeProvider(
        [
            [
                text_response(
                    "ok",
                    usage=Usage(
                        input_tokens=10,
                        output_tokens=5,
                        cache_read_tokens=10_000,
                    ),
                )
            ]
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, estimator=estimator)
    await _consume(runtime, "go")
    # The prompt was ~10k tokens with most of it cached. Counting only the
    # uncached input would drive the factor down toward the 0.5 clamp.
    assert estimator.factor > 1.0
    await runtime.close()


async def test_context_tokens_track_the_current_view(tmp_path: Path) -> None:
    provider = FakeProvider([[text_response("ok")]])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    initial = runtime.context_tokens
    assert initial > 0  # system prompt + tools, before the first request
    await _consume(runtime, "hello")
    after_turn = runtime.context_tokens
    assert after_turn > initial
    assert runtime.context_window == 200_000
    await runtime.clear()
    assert runtime.context_tokens > 0
    assert runtime.context_tokens < after_turn
    await runtime.close()


async def test_memory_rides_on_the_user_message_not_the_system_prompt(
    tmp_path: Path,
) -> None:
    from dcx.context.memory import MemoryRecord, MemoryStore

    store = MemoryStore(tmp_path / "memory")
    store.save(MemoryRecord("prefers-tabs", "indent", "user", "Use tabs."))
    provider = FakeProvider([[text_response("ok")]])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, memory=store)
    await _consume(runtime, "tabs preference")

    request = provider.requests[0]
    system_text = "\n".join(section.text for section in request.system)
    assert "Memory catalog" not in system_text
    assert "call Remember" in system_text

    last_user = [message for message in request.messages if message.role == "user"][-1]
    assert "[Memory]" in last_user.text()
    assert "Use tabs." in last_user.text()
    # The stored transcript keeps the original message only.
    assert "[Memory]" not in runtime.messages[0].text()
    await runtime.close()


async def test_memory_note_is_absent_without_records(tmp_path: Path) -> None:
    from dcx.context.memory import MemoryStore

    provider = FakeProvider([[text_response("ok")]])
    runtime, _ = make_runtime(
        provider, tmp_path=tmp_path, memory=MemoryStore(tmp_path / "memory")
    )
    await _consume(runtime, "hello")
    last_user = [m for m in provider.requests[0].messages if m.role == "user"][-1]
    assert "[Memory]" not in last_user.text()
    await runtime.close()


async def test_saving_memory_leaves_the_system_prompt_unchanged(tmp_path: Path) -> None:
    from dcx.context.memory import MemoryStore
    from dcx.tools.builtin.memory import RememberTool

    store = MemoryStore(tmp_path / "memory")
    provider = FakeProvider(
        [
            [
                tool_response(
                    [
                        (
                            "t1",
                            "Remember",
                            {
                                "name": "prefers-tabs",
                                "type": "user",
                                "content": "Use tabs.",
                            },
                        )
                    ]
                )
            ],
            [text_response("saved")],
            [text_response("onward")],
        ]
    )
    runtime, _ = make_runtime(
        provider,
        tmp_path=tmp_path,
        tools=[RememberTool(store)],
        memory=store,
    )
    await _consume(runtime, "remember that I use tabs")
    await _consume(runtime, "next question")

    first = "\n".join(section.text for section in provider.requests[0].system)
    third = "\n".join(section.text for section in provider.requests[2].system)
    # The cached prefix must survive a memory write.
    assert first == third
    assert "prefers-tabs" not in first

    last_user = [m for m in provider.requests[2].messages if m.role == "user"][-1]
    assert "prefers-tabs" in last_user.text()
    assert "Use tabs." in last_user.text()
    await runtime.close()


def _request_signature(messages):
    import json

    from dcx.messages import TextBlock, ToolResultBlock, ToolUseBlock

    signature = []
    for message in messages:
        blocks = []
        for block in message.content:
            if isinstance(block, TextBlock):
                blocks.append(("text", block.text))
            elif isinstance(block, ToolUseBlock):
                blocks.append(
                    (
                        "tool",
                        block.id,
                        block.name,
                        json.dumps(dict(block.input), sort_keys=True),
                    )
                )
            elif isinstance(block, ToolResultBlock):
                blocks.append(
                    (
                        "result",
                        block.tool_use_id,
                        tuple(item.text for item in block.content),
                    )
                )
        signature.append((message.role, tuple(blocks)))
    return signature


async def test_requests_append_without_rewriting_the_prefix(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            [text_response("first answer")],
            [tool_response([("t1", "Echo", {"text": "hi"})])],
            [text_response("done")],
            [text_response("third answer")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    await _consume(runtime, "one")
    await _consume(runtime, "two")
    await _consume(runtime, "three")

    systems = ["\n\n".join(section.text for section in r.system) for r in provider.requests]
    assert len(set(systems)) == 1

    signatures = [_request_signature(r.messages) for r in provider.requests]
    for earlier, later in zip(signatures, signatures[1:], strict=False):
        # Every request must extend the previous one byte for byte; a rewrite
        # here would invalidate the provider's cached prefix.
        assert later[: len(earlier)] == earlier
    await runtime.close()


async def test_refusal_stop_reason_is_preserved(tmp_path: Path) -> None:
    provider = FakeProvider([[text_response("no", stop_reason="refusal")]])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    events = [event async for event in runtime.run_turn("go")]
    assert any(isinstance(event, AgentError) and event.kind == "refusal" for event in events)
    assert isinstance(events[-1], AgentTurnFinished)
    assert events[-1].stop_reason == "refusal"
    await runtime.close()


async def test_error_stop_reason_is_preserved(tmp_path: Path) -> None:
    provider = FakeProvider([[text_response("bad", stop_reason="error")]])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    events = [event async for event in runtime.run_turn("go")]
    assert any(isinstance(event, AgentError) and event.kind == "error" for event in events)
    assert isinstance(events[-1], AgentTurnFinished)
    assert events[-1].stop_reason == "error"
    await runtime.close()


async def test_max_tokens_exhaustion_is_reported(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            [text_response("a", stop_reason="max_tokens")],
            [text_response("b", stop_reason="max_tokens")],
            [text_response("c", stop_reason="max_tokens")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    events = [event async for event in runtime.run_turn("go")]
    assert any(isinstance(event, AgentNotice) and "Output limit" in event.text for event in events)
    assert isinstance(events[-1], AgentTurnFinished)
    assert events[-1].stop_reason == "max_tokens"
    assert len(provider.requests) == 3

    await runtime.close()


async def test_empty_delta_still_retries(tmp_path: Path, monkeypatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    provider = FakeProvider(
        [[TextDelta(""), fail("rate limited", "retryable")], [text_response("ok")]]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    events = [event async for event in runtime.run_turn("go")]
    assert len(provider.requests) == 2
    assert not any(isinstance(event, AgentError) for event in events)
    await runtime.close()


async def test_post_completion_error_is_ignored(tmp_path: Path) -> None:
    provider = FakeProvider(
        [[text_response("hi"), fail("rate limited", "retryable")]]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    events = [event async for event in runtime.run_turn("go")]
    assert len(provider.requests) == 1
    assert not any(isinstance(event, AgentError) for event in events)
    assert [message.role for message in runtime.messages] == ["user", "assistant"]
    await runtime.close()


async def test_cancel_during_permission_ask_closes_the_tool_pair(
    tmp_path: Path,
) -> None:
    class HangingAsker:
        async def ask(self, request):  # noqa: ANN001
            await asyncio.Event().wait()

    provider = FakeProvider(
        [[tool_response([("t1", "Bash", {"command": "echo hi"})])]]
    )
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    runtime, _ = make_runtime(
        provider,
        tmp_path=tmp_path,
        tools=[ShellTool()],
        policy=policy,
        asker=HangingAsker(),
    )
    task = asyncio.create_task(_consume(runtime, "go"))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("turn should have been cancelled")
    result = runtime.messages[-1].tool_results()[0]
    assert result.tool_use_id == "t1"
    assert result.is_error
    assert "cancelled" in result.content[0].text
    await runtime.close()


async def test_image_is_part_of_the_user_message(tmp_path: Path) -> None:
    provider = FakeProvider([[text_response("seen")]])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    image = ImageBlock("image/png", "aGVsbG8=")
    await _consume(runtime, "look")
    await runtime.close()

    runtime, _ = make_runtime(FakeProvider([[text_response("seen")]]), tmp_path=tmp_path / "two")
    events = [
        event
        async for event in runtime.run_turn("look", images=(image,))
    ]
    user = runtime.messages[0]
    assert user.role == "user"
    assert any(isinstance(block, ImageBlock) for block in user.content)
    assert any(isinstance(event, AgentTurnFinished) for event in events)
    await runtime.close()


async def test_context_tokens_estimates_before_the_first_request(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(FakeProvider([]), tmp_path=tmp_path)
    try:
        assert runtime.context_tokens > 0
        assert runtime.context_tokens < runtime.context_window
    finally:
        await runtime.close()


async def test_write_tool_creates_a_restorable_checkpoint(tmp_path: Path) -> None:
    from dcx.tools.builtin.files import WriteTool

    provider = FakeProvider(
        [
            [tool_response([("t1", "Write", {"path": "note.txt", "content": "one"})])],
            [text_response("done")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[WriteTool()])
    await _consume(runtime, "write a note")
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "one"

    preview = runtime.preview_checkpoints(last_user=True)
    assert preview.has_changes
    assert preview.files[0].path == "note.txt"
    assert preview.files[0].status == "created"

    result = runtime.restore_checkpoints(last_user=True)
    assert result.restored == ("note.txt",)
    assert not (tmp_path / "note.txt").exists()
    await runtime.close()


async def test_restore_can_keep_the_conversation(tmp_path: Path) -> None:
    from dcx.tools.builtin.files import WriteTool

    provider = FakeProvider(
        [
            [tool_response([("t1", "Write", {"path": "note.txt", "content": "one"})])],
            [text_response("done")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[WriteTool()])
    await _consume(runtime, "write a note")
    before = runtime.messages

    result = runtime.restore_checkpoints(last_user=True)
    assert result.restored == ("note.txt",)
    assert not (tmp_path / "note.txt").exists()
    assert runtime.messages == before
    # The manifest now records the restored state, so a repeat is a no-op.
    assert not runtime.preview_checkpoints(last_user=True).files
    await runtime.close()


async def test_bad_checkpoint_path_does_not_break_the_turn(tmp_path: Path) -> None:
    from dcx.tools.builtin.files import WriteTool

    provider = FakeProvider(
        [
            [
                tool_response(
                    [("t1", "Write", {"path": "~definitely-no-such-user/x", "content": "x"})]
                )
            ],
            [text_response("done")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[WriteTool()])
    events = [event async for event in runtime.run_turn("write")]
    finished = [event for event in events if isinstance(event, AgentToolFinished)]
    assert finished and finished[0].is_error
    assert isinstance(events[-1], AgentTurnFinished)
    await runtime.close()


async def test_continuing_same_turn_keeps_earlier_checkpoint(tmp_path: Path) -> None:
    from dcx.tools.builtin.files import WriteTool

    provider = FakeProvider(
        [
            [tool_response([("t1", "Write", {"path": "a.txt", "content": "a"})])],
            [text_response("first")],
            [tool_response([("t2", "Write", {"path": "b.txt", "content": "b"})])],
            [text_response("second")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[WriteTool()])
    await _consume(runtime, "first")
    async for _ in runtime.run_turn(None):
        pass
    preview = runtime.preview_checkpoints(last_user=True)
    assert {item.path for item in preview.files} == {"a.txt", "b.txt"}
    await runtime.close()


async def test_edit_rewind_discards_orphan_checkpoints(tmp_path: Path) -> None:
    from dcx.tools.builtin.files import WriteTool

    provider = FakeProvider(
        [
            [tool_response([("t1", "Write", {"path": "a.txt", "content": "a"})])],
            [text_response("done")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[WriteTool()])
    await _consume(runtime, "write")
    user_id = runtime.messages[0].id
    await runtime.rewind_to(user_id)
    assert not list((tmp_path / "session" / "checkpoints" / "turns").glob("*.json"))
    await runtime.close()
