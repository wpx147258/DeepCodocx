from __future__ import annotations

from pathlib import Path

from cli_agent.assembly import _make_completer
from cli_agent.providers.base import ModelProfile
from cli_agent.runtime import ProviderSlot
from tests.support.fake_provider import FakeProvider, text_response
from tests.support.runtime_fixture import make_runtime


def _profile(name: str, model: str) -> ModelProfile:
    return ModelProfile(name, "anthropic_compat", model, 1_000, 100)


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
