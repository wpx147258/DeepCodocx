from __future__ import annotations

import base64
from pathlib import Path

from cli_agent.assembly import _make_completer
from cli_agent.messages import IMAGE_UNSUPPORTED_NOTE, ImageBlock
from cli_agent.providers.base import ModelProfile
from cli_agent.runtime import ProviderSlot
from cli_agent.tools.builtin.files import ReadTool
from tests.support.fake_provider import FakeProvider, text_response, tool_response
from tests.support.runtime_fixture import make_runtime


def _profile(
    name: str, model: str, *, supports_images: bool = False
) -> ModelProfile:
    return ModelProfile(
        name,
        "anthropic_compat",
        model,
        1_000,
        100,
        supports_images=supports_images,
    )


async def test_completer_follows_slot_replacement() -> None:
    first = FakeProvider([[text_response("one")]])
    second = FakeProvider([[text_response("two")]])
    slot = ProviderSlot(first, _profile("mock1", "model-1"))
    complete = _make_completer(slot)

    assert await complete("hi", 100) == "one"
    slot.replace(second, _profile("mock2", "model-2"), None)
    assert await complete("hi", 100) == "two"

    assert first.requests[0].model == "model-1"
    assert second.requests[0].model == "model-2"


async def test_switch_model_updates_shared_slot(tmp_path: Path) -> None:
    old = FakeProvider([[text_response("old")]])
    runtime, _ = make_runtime(old, tmp_path=tmp_path)
    new = FakeProvider([[text_response("new")]])
    new_profile = _profile("mock2", "model-2")

    runtime._deps.provider_factory = lambda name, model: (new, new_profile, None)
    await runtime.switch_model("mock2", "model-2")

    assert runtime._deps.provider_slot is not None
    assert runtime._deps.provider_slot.provider is new
    assert runtime.provider_name == "mock2"
    assert old.closed
    await runtime.close()


async def _consume(runtime, text: str) -> None:
    async for _ in runtime.run_turn(text):
        pass


async def test_switch_model_enables_image_input(tmp_path: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\npayload"
    (tmp_path / "pic.png").write_bytes(payload)
    old = FakeProvider([[text_response("old")]])
    new = FakeProvider(
        [
            [tool_response([("t1", "Read", {"path": "pic.png"})])],
            [text_response("done")],
        ]
    )
    runtime, _ = make_runtime(old, tmp_path=tmp_path, tools=[ReadTool()])
    new_profile = _profile("mock2", "model-2", supports_images=True)
    runtime._deps.provider_factory = lambda name, model: (new, new_profile, None)
    await runtime.switch_model("mock2", "model-2")

    await _consume(runtime, "look at the image")
    result = runtime.messages[2].tool_results()[0]
    images = [block for block in result.content if isinstance(block, ImageBlock)]
    assert len(images) == 1
    assert base64.b64decode(images[0].data) == payload
    await runtime.close()


async def test_switch_model_disables_image_input(tmp_path: Path) -> None:
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\npayload")
    old = FakeProvider([[text_response("old")]])
    new = FakeProvider(
        [
            [tool_response([("t1", "Read", {"path": "pic.png"})])],
            [text_response("done")],
        ]
    )
    initial = _profile("mock1", "model-1", supports_images=True)
    runtime, _ = make_runtime(
        old, tmp_path=tmp_path, tools=[ReadTool()], profile=initial
    )
    new_profile = _profile("mock2", "model-2", supports_images=False)
    runtime._deps.provider_factory = lambda name, model: (new, new_profile, None)
    await runtime.switch_model("mock2", "model-2")

    await _consume(runtime, "look at the image")
    result = runtime.messages[2].tool_results()[0]
    assert result.content[0].text == IMAGE_UNSUPPORTED_NOTE
    assert not any(isinstance(block, ImageBlock) for block in result.content)
    await runtime.close()


async def test_completer_caps_max_tokens_to_profile() -> None:
    provider = FakeProvider([[text_response("one")], [text_response("two")]])
    profile = ModelProfile("mock", "anthropic_compat", "model-1", 1_000, 128)
    slot = ProviderSlot(provider, profile)
    complete = _make_completer(slot)

    assert await complete("hi", 2_000) == "one"
    assert await complete("hi", 64) == "two"
    assert provider.requests[0].max_output_tokens == 128
    assert provider.requests[1].max_output_tokens == 64


async def test_completer_skips_when_output_limit_is_too_small() -> None:
    provider = FakeProvider([])
    profile = ModelProfile("mock", "anthropic_compat", "model-1", 1_000, 32)
    complete = _make_completer(ProviderSlot(provider, profile))

    assert await complete("hi", 2_000) == ""
    assert provider.requests == []


async def test_completer_follows_profile_output_limits() -> None:
    provider = FakeProvider([[text_response("one")], [text_response("two")]])
    slot = ProviderSlot(
        provider, ModelProfile("mock", "anthropic_compat", "model-1", 1_000, 2_000)
    )
    complete = _make_completer(slot)

    assert await complete("hi", 2_000) == "one"
    slot.replace(
        provider,
        ModelProfile("mock", "anthropic_compat", "model-2", 1_000, 128),
        None,
    )
    assert await complete("hi", 2_000) == "two"
    assert provider.requests[0].max_output_tokens == 2_000
    assert provider.requests[1].max_output_tokens == 128
