from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import cli_agent.assembly as assembly
from cli_agent.assembly import BuildOptions, build_runtime
from cli_agent.events import AgentContextEvent
from cli_agent.providers.base import ModelProfile
from tests.support.fake_provider import FakeProvider, text_response


def _profile(name: str, model: str) -> ModelProfile:
    return ModelProfile(
        provider_name=name,
        provider_kind="anthropic_compat",
        model=model,
        context_window=200_000,
        max_output_tokens=8_192,
    )


def _summarizer_requests(provider: FakeProvider) -> list[Any]:
    return [
        request
        for request in provider.requests
        if request.system and request.system[0].name == "summarizer"
    ]


def _build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripts: dict[str, list[list[Any]]],
) -> tuple[Any, dict[str, tuple[FakeProvider, ModelProfile]]]:
    providers = {
        "deepseek": (FakeProvider(scripts["deepseek"]), _profile("deepseek", "m1")),
        "anthropic": (FakeProvider(scripts["anthropic"]), _profile("anthropic", "m2")),
    }

    def fake_build(
        config: Any,
        *,
        provider_name: str | None = None,
        model: str | None = None,
        thinking_budget: int | None = None,
        env: Any = None,
    ) -> tuple[FakeProvider, ModelProfile]:
        provider, profile = providers[provider_name or "deepseek"]
        return provider, replace(profile, model=model or profile.model)

    monkeypatch.setattr(assembly, "build_provider", fake_build)
    options = BuildOptions(
        provider="deepseek",
        model="m1",
        persist=False,
        thinking_enabled=False,
        env={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
    )
    return build_runtime(options), providers


async def test_switch_model_retires_the_old_provider_and_repoints_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_text = "line of work " * 12
    bundle, providers = _build(
        tmp_path,
        monkeypatch,
        {
            "deepseek": [[text_response(long_text)] for _ in range(3)],
            "anthropic": [[text_response("compact summary " * 20)]],
        },
    )
    runtime = bundle.runtime
    old_provider, _ = providers["deepseek"]
    new_provider, _ = providers["anthropic"]

    for index in range(3):
        async for _ in runtime.run_turn(f"request {index} " + "context " * 8):
            pass

    old_usage = len(old_provider.requests)
    await runtime.switch_model("anthropic", "m2")
    assert runtime.provider_name == "anthropic"
    assert runtime.model == "m2"
    # A background summary or extraction may still be holding the old client.
    assert not old_provider.closed

    events = [event async for event in runtime.compact()]
    assert any(
        isinstance(event, AgentContextEvent) and event.kind == "compact"
        for event in events
    )
    # Compaction now runs on the active provider, not the stale one.
    assert _summarizer_requests(new_provider)
    assert _summarizer_requests(old_provider) == []
    assert len(old_provider.requests) == old_usage

    await runtime.close()
    assert old_provider.closed
    assert new_provider.closed
