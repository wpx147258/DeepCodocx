from __future__ import annotations

import pytest

from dcx.errors import ProviderFailure
from dcx.events import AgentTextDelta, AgentToolStarted
from dcx.messages import Usage
from dcx.providers.base import ModelProfile, ModelRequest, ReasoningDelta, TextDelta, ToolCall
from dcx.runtime_attempt import Attempt, stream_attempt
from tests.support.fake_provider import FakeProvider, fail, text_response, tool_response


def _request() -> ModelRequest:
    return ModelRequest(
        model="mock-1",
        system=(),
        messages=(),
        tools=(),
        max_output_tokens=100,
        profile=ModelProfile(
            provider_name="mock",
            provider_kind="anthropic_compat",
            model="mock-1",
            context_window=1000,
            max_output_tokens=100,
        ),
    )


async def test_attempt_records_deltas_and_completion() -> None:
    provider = FakeProvider(
        [[TextDelta("he"), TextDelta("llo"), text_response("hello")]]
    )
    attempt = Attempt()
    events = [event async for event in stream_attempt(provider, _request(), attempt)]

    assert [event.text for event in events if isinstance(event, AgentTextDelta)] == [
        "he",
        "llo",
    ]
    assert attempt.assistant is not None
    assert attempt.assistant.text() == "hello"
    assert attempt.emitted_text is True
    assert attempt.stop_reason == "end_turn"
    assert attempt.usage == Usage(input_tokens=10, output_tokens=5)


async def test_attempt_falls_back_to_final_message_text() -> None:
    provider = FakeProvider([[text_response("the whole answer")]])
    attempt = Attempt()
    events = [event async for event in stream_attempt(provider, _request(), attempt)]

    assert [event.text for event in events if isinstance(event, AgentTextDelta)] == [
        "the whole answer"
    ]
    assert attempt.emitted_text is False


async def test_attempt_marks_reasoning_deltas() -> None:
    provider = FakeProvider(
        [[ReasoningDelta("hmm"), TextDelta("answer"), text_response("answer")]]
    )
    attempt = Attempt()
    events = [event async for event in stream_attempt(provider, _request(), attempt)]

    reasoning = [
        event for event in events if isinstance(event, AgentTextDelta) and event.reasoning
    ]
    assert [event.text for event in reasoning] == ["hmm"]


async def test_attempt_announces_tool_calls_and_parse_errors() -> None:
    provider = FakeProvider(
        [
            [
                ToolCall("t1", "Echo", {"text": "x"}, parse_error="bad json"),
                tool_response([("t1", "Echo", {"text": "x"})]),
            ]
        ]
    )
    attempt = Attempt()
    events = [event async for event in stream_attempt(provider, _request(), attempt)]

    started = [event for event in events if isinstance(event, AgentToolStarted)]
    assert [event.tool_use_id for event in started] == ["t1"]
    assert attempt.parse_errors == {"t1": "bad json"}
    assert attempt.stop_reason == "tool_use"


async def test_attempt_maps_provider_error_to_failure() -> None:
    provider = FakeProvider([[fail("overloaded", "retryable")]])
    attempt = Attempt()
    with pytest.raises(ProviderFailure) as excinfo:
        [event async for event in stream_attempt(provider, _request(), attempt)]

    assert excinfo.value.kind == "retryable"
    assert excinfo.value.message == "overloaded"
    assert attempt.assistant is None
