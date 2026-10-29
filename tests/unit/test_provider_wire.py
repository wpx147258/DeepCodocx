from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from dcx.messages import (
    ImageBlock,
    Message,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    assistant_message,
    now_iso,
)
from dcx.providers.anthropic_compat import (
    AnthropicCompatProvider,
    _final_blocks,
    _final_usage,
    _messages_to_wire,
    _stop_reason,
)
from dcx.providers.base import (
    ModelProfile,
    ModelRequest,
    ReasoningDelta,
    ResponseCompleted,
    SystemSection,
    TextDelta,
    ThinkingConfig,
    ToolCall,
)
from dcx.tools.base import ToolSpec


def _profile(**overrides) -> ModelProfile:
    profile = ModelProfile(
        provider_name="mock",
        provider_kind="anthropic_compat",
        model="mock-1",
        context_window=1000,
        max_output_tokens=100,
        supports_images=False,
    )
    return replace(profile, **overrides)


def test_reasoning_is_omitted_by_default() -> None:
    message = assistant_message(
        [
            ReasoningBlock("think", {"signature": "sig"}),
            TextBlock("hello"),
            ToolUseBlock("t1", "Echo", {"text": "x"}),
        ]
    )
    wire = _messages_to_wire((message,), _profile())
    assert wire[0]["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "t1", "name": "Echo", "input": {"text": "x"}},
    ]


def test_reasoning_replays_when_requested() -> None:
    message = assistant_message([ReasoningBlock("think", {"signature": "sig"})])
    wire = _messages_to_wire((message,), _profile(replay_reasoning=True))
    assert wire[0]["content"] == [
        {"type": "thinking", "thinking": "think", "signature": "sig"}
    ]


def test_image_is_downgraded_when_unsupported() -> None:
    message = Message(
        id="m1",
        role="user",
        content=(ImageBlock("image/png", "abc"),),
        created_at=now_iso(),
    )
    wire = _messages_to_wire((message,), _profile())
    assert wire[0]["content"][0]["type"] == "text"
    assert "image omitted" in wire[0]["content"][0]["text"]


def test_tool_result_wire_shape() -> None:
    message = Message(
        id="m1",
        role="user",
        content=(ToolResultBlock("t1", (TextBlock("ok"),), is_error=True),),
        created_at=now_iso(),
    )
    wire = _messages_to_wire((message,), _profile())
    assert wire[0]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [{"type": "text", "text": "ok"}],
            "is_error": True,
        }
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "end_turn"),
        ("end_turn", "end_turn"),
        ("stop_sequence", "end_turn"),
        ("tool_use", "tool_use"),
        ("max_tokens", "max_tokens"),
        ("refusal", "refusal"),
        ("content_filter", "refusal"),
        ("something_new", "error"),
    ],
)
def test_stop_reason_mapping(raw, expected) -> None:
    assert _stop_reason(raw)[0] == expected


def test_final_blocks_and_usage() -> None:
    final = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="hello"),
            SimpleNamespace(type="thinking", thinking="hmm", signature="sig"),
            SimpleNamespace(type="tool_use", id="t1", name="Echo", input={"x": 1}),
        ],
        usage=SimpleNamespace(
            input_tokens=11,
            output_tokens=7,
            cache_read_input_tokens=3,
            cache_creation_input_tokens=2,
        ),
    )
    blocks = _final_blocks(final)
    assert isinstance(blocks[0], TextBlock)
    assert isinstance(blocks[1], ReasoningBlock)
    assert isinstance(blocks[2], ToolUseBlock)
    usage = _final_usage(final)
    assert usage.input_tokens == 11
    assert usage.output_tokens == 7
    assert usage.cache_read_tokens == 3
    assert usage.cache_write_tokens == 2


class _FakeStream:
    def __init__(self, events, final) -> None:
        self.events = events
        self.final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    def __aiter__(self):
        async def iterator():
            for event in self.events:
                yield event

        return iterator()

    async def get_final_message(self):
        return self.final


class _FakeMessages:
    def __init__(self, stream) -> None:
        self._stream = stream
        self.params = None

    def stream(self, **params):
        self.params = params
        return self._stream


class _FakeClient:
    def __init__(self, stream) -> None:
        self.messages = _FakeMessages(stream)

    async def close(self) -> None:
        return None


async def test_streaming_maps_to_normalized_events() -> None:
    final = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="hello"),
            SimpleNamespace(type="tool_use", id="t1", name="Echo", input={"text": "x"}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=5,
            output_tokens=6,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    events = [
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="hel")),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="lo")),
        SimpleNamespace(
            type="content_block_stop",
            content_block=SimpleNamespace(type="tool_use", id="t1", name="Echo", input={"text": "x"}),
        ),
    ]
    client = _FakeClient(_FakeStream(events, final))
    provider = AnthropicCompatProvider(api_key="test", client=client)
    tool = ToolSpec(
        name="Echo",
        description="Echo",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )
    request = ModelRequest(
        model="mock-1",
        system=(SystemSection("identity", "be useful"),),
        messages=(Message("u1", "user", (TextBlock("hi"),), now_iso()),),
        tools=(tool,),
        max_output_tokens=100,
        profile=_profile(),
    )
    collected = [event async for event in provider.stream(request)]
    assert [type(event) for event in collected] == [TextDelta, TextDelta, ToolCall, ResponseCompleted]
    assert collected[0].text + collected[1].text == "hello"
    assert collected[2].tool_use_id == "t1"
    completed = collected[3]
    assert completed.stop_reason == "tool_use"
    assert completed.usage.output_tokens == 6
    assert client.messages.params["system"] == "be useful"
    assert client.messages.params["tools"][0]["name"] == "Echo"


async def test_streaming_ignores_unknown_events() -> None:
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    events = [
        SimpleNamespace(type="thinking", thinking="ignored"),
        SimpleNamespace(type="input_json", partial_json="{}"),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="ok")),
    ]
    client = _FakeClient(_FakeStream(events, final))
    provider = AnthropicCompatProvider(api_key="test", client=client)
    request = ModelRequest(
        model="mock-1",
        system=(),
        messages=(Message("u1", "user", (TextBlock("hi"),), now_iso()),),
        tools=(),
        max_output_tokens=100,
        profile=_profile(),
    )
    collected = [event async for event in provider.stream(request)]
    assert isinstance(collected[-1], ResponseCompleted)
    assert any(isinstance(event, TextDelta) and event.text == "ok" for event in collected)


async def test_streaming_maps_thinking_delta_and_block() -> None:
    final = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="hmm", signature="sig"),
            SimpleNamespace(type="text", text="answer"),
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
    )
    events = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking="hmm"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="answer"),
        ),
    ]
    client = _FakeClient(_FakeStream(events, final))
    provider = AnthropicCompatProvider(api_key="test", client=client)
    request = ModelRequest(
        model="mock-1",
        system=(),
        messages=(Message("u1", "user", (TextBlock("hi"),), now_iso()),),
        tools=(),
        max_output_tokens=100,
        profile=_profile(),
    )
    collected = [event async for event in provider.stream(request)]
    assert any(
        isinstance(event, ReasoningDelta) and event.text == "hmm" for event in collected
    )
    completed = collected[-1]
    assert isinstance(completed, ResponseCompleted)
    assert any(isinstance(block, ReasoningBlock) for block in completed.message.content)


async def test_thinking_request_params_and_no_temperature() -> None:
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    client = _FakeClient(_FakeStream([], final))
    provider = AnthropicCompatProvider(api_key="test", client=client)
    request = ModelRequest(
        model="mock-1",
        system=(),
        messages=(Message("u1", "user", (TextBlock("hi"),), now_iso()),),
        tools=(),
        max_output_tokens=4_096,
        profile=_profile(),
        thinking=ThinkingConfig(2_048),
        temperature=0.3,
    )
    collected = [event async for event in provider.stream(request)]
    assert isinstance(collected[-1], ResponseCompleted)
    assert client.messages.params["thinking"] == {
        "type": "enabled",
        "budget_tokens": 2_048,
    }
    assert "temperature" not in client.messages.params


def test_redacted_thinking_replays() -> None:
    message = assistant_message([ReasoningBlock("", {"redacted": "abc"})])
    wire = _messages_to_wire((message,), _profile(replay_reasoning=True))
    assert wire[0]["content"] == [{"type": "redacted_thinking", "data": "abc"}]


def test_reasoning_without_signature_is_omitted() -> None:
    message = assistant_message([ReasoningBlock("think", {})])
    wire = _messages_to_wire((message,), _profile(replay_reasoning=True))
    assert all(item["type"] != "thinking" for item in wire[0]["content"])


async def test_prompt_caching_marks_stable_prefix_and_tools() -> None:
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    client = _FakeClient(_FakeStream([], final))
    provider = AnthropicCompatProvider(api_key="test", client=client)
    tool = ToolSpec(
        name="Read",
        description="Read",
        input_schema={"type": "object", "properties": {}},
    )
    request = ModelRequest(
        model="mock-1",
        system=(
            SystemSection("identity", "stable instructions", cacheable=True),
            SystemSection("memory", "dynamic memory"),
        ),
        messages=(Message("u1", "user", (TextBlock("hi"),), now_iso()),),
        tools=(tool,),
        max_output_tokens=100,
        profile=_profile(prompt_caching=True),
    )
    [event async for event in provider.stream(request)]
    assert client.messages.params["system"] == [
        {
            "type": "text",
            "text": "stable instructions",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "dynamic memory"},
    ]
    assert client.messages.params["tools"][-1]["cache_control"] == {"type": "ephemeral"}


async def test_prompt_caching_is_opt_in() -> None:
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    client = _FakeClient(_FakeStream([], final))
    provider = AnthropicCompatProvider(api_key="test", client=client)
    tool = ToolSpec(
        name="Read",
        description="Read",
        input_schema={"type": "object", "properties": {}},
    )
    request = ModelRequest(
        model="mock-1",
        system=(
            SystemSection("identity", "stable instructions", cacheable=True),
            SystemSection("memory", "dynamic memory"),
        ),
        messages=(Message("u1", "user", (TextBlock("hi"),), now_iso()),),
        tools=(tool,),
        max_output_tokens=100,
        profile=_profile(),
    )
    [event async for event in provider.stream(request)]
    assert client.messages.params["system"] == "stable instructions\n\ndynamic memory"
    assert "cache_control" not in client.messages.params["tools"][0]


def test_image_is_forwarded_when_supported() -> None:
    message = Message(
        id="m1",
        role="user",
        content=(ImageBlock("image/png", "abc"),),
        created_at=now_iso(),
    )
    wire = _messages_to_wire((message,), _profile(supports_images=True))
    assert wire[0]["content"] == [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "abc"},
        }
    ]


def test_tool_result_image_is_forwarded_when_supported() -> None:
    message = Message(
        id="m1",
        role="user",
        content=(ToolResultBlock("t1", (ImageBlock("image/png", "abc"),)),),
        created_at=now_iso(),
    )
    wire = _messages_to_wire((message,), _profile(supports_images=True))
    assert wire[0]["content"][0]["content"][0]["type"] == "image"
    assert wire[0]["content"][0]["is_error"] is False
