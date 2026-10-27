from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import cli_agent.assembly as assembly
from cli_agent.assembly import BuildOptions, build_runtime
from cli_agent.errors import ConfigError
from cli_agent.providers.base import ModelProfile
from tests.support.fake_provider import FakeProvider, text_response


def _install(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    profile = ModelProfile("deepseek", "anthropic_compat", "m1", 200_000, 8_192)

    def fake_build(
        config: Any,
        *,
        provider_name: str | None = None,
        model: str | None = None,
        thinking_budget: int | None = None,
        env: Any = None,
    ) -> tuple[FakeProvider, ModelProfile]:
        return provider, replace(profile, model=model or profile.model)

    monkeypatch.setattr(assembly, "build_provider", fake_build)


def _options(tmp_path: Path, **overrides: Any) -> BuildOptions:
    values: dict[str, Any] = {
        "provider": "deepseek",
        "model": "m1",
        "persist": True,
        "thinking_enabled": False,
        "env": {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
    }
    values.update(overrides)
    return BuildOptions(**values)


def _messages(path: Path) -> list[dict[str, Any]]:
    records = (path / "session.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in records if line.strip()]


async def test_resume_fork_continues_in_a_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider(
        [[text_response("first answer")], [text_response("second answer")]]
    )
    _install(monkeypatch, provider)

    bundle = build_runtime(_options(tmp_path))
    async for _ in bundle.runtime.run_turn("hello"):
        pass
    source_id = bundle.session_id
    paths = bundle.config.paths
    await bundle.runtime.close()

    source_jsonl = paths.session_dir(source_id) / "session.jsonl"
    before_fork = source_jsonl.read_text(encoding="utf-8").splitlines()

    resumed = build_runtime(_options(tmp_path, resume_id=source_id, fork=True))
    try:
        assert resumed.forked_from == source_id
        assert resumed.session_id != source_id
        assert resumed.resumed
        assert [message.role for message in resumed.runtime.messages] == [
            "user",
            "assistant",
        ]
        assert resumed.runtime.messages[0].text() == "hello"

        async for _ in resumed.runtime.run_turn("next"):
            pass
    finally:
        await resumed.runtime.close()

    # The new turn was written to the copy, never to the source.
    assert source_jsonl.read_text(encoding="utf-8").splitlines() == before_fork
    fork_messages = [
        record
        for record in _messages(paths.session_dir(resumed.session_id))
        if record.get("type") == "message"
    ]
    assert len(fork_messages) == 4
    assert fork_messages[-2]["message"]["content"][0]["text"] == "next"


def test_fork_requires_a_resume_target(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="requires"):
        build_runtime(_options(tmp_path, persist=False, fork=True))
