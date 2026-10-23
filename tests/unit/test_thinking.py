from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent.assembly import BuildOptions, build_runtime
from cli_agent.errors import ConfigError
from cli_agent.providers.base import ThinkingConfig


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    values = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    values.update(extra)
    return values


async def test_thinking_enabled_for_anthropic(tmp_path: Path) -> None:
    bundle = build_runtime(
        BuildOptions(
            provider="anthropic",
            model="claude-sonnet-4-6",
            thinking_enabled=True,
            thinking_budget=2_048,
            persist=False,
            cwd=tmp_path,
            env=_env(tmp_path, ANTHROPIC_API_KEY="sk-test"),
        )
    )
    try:
        assert bundle.runtime.thinking == ThinkingConfig(2_048)
        assert bundle.profile.supports_thinking
        assert bundle.profile.replay_reasoning
    finally:
        await bundle.runtime.close()


def test_thinking_rejected_for_unsupported_provider(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not support extended thinking"):
        build_runtime(
            BuildOptions(
                provider="kimi",
                model="kimi-k2.7-code",
                thinking_enabled=True,
                persist=False,
                cwd=tmp_path,
                env=_env(tmp_path, MOONSHOT_API_KEY="sk-test"),
            )
        )


def test_thinking_budget_must_fit_max_output(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="smaller than max_output_tokens"):
        build_runtime(
            BuildOptions(
                provider="anthropic",
                model="claude-sonnet-4-6",
                thinking_enabled=True,
                thinking_budget=20_000,
                persist=False,
                cwd=tmp_path,
                env=_env(tmp_path, ANTHROPIC_API_KEY="sk-test"),
            )
        )


async def test_deepseek_defaults_to_thinking(tmp_path: Path) -> None:
    bundle = build_runtime(
        BuildOptions(
            provider="deepseek",
            persist=False,
            cwd=tmp_path,
            env=_env(tmp_path, DEEPSEEK_API_KEY="sk-test"),
        )
    )
    try:
        assert bundle.profile.model == "deepseek-flash[1M]"
        assert bundle.runtime.thinking == ThinkingConfig(4_096)
        assert bundle.profile.replay_reasoning
    finally:
        await bundle.runtime.close()


async def test_can_disable_provider_default_thinking(tmp_path: Path) -> None:
    bundle = build_runtime(
        BuildOptions(
            provider="deepseek",
            thinking_enabled=False,
            persist=False,
            cwd=tmp_path,
            env=_env(tmp_path, DEEPSEEK_API_KEY="sk-test"),
        )
    )
    try:
        assert bundle.runtime.thinking is None
        assert not bundle.profile.replay_reasoning
    finally:
        await bundle.runtime.close()
