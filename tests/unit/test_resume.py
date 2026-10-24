from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent import assembly
from cli_agent.assembly import BuildOptions, RuntimeBundle, build_runtime
from cli_agent.config import AppPaths
from cli_agent.errors import ConfigError
from cli_agent.providers.base import ModelProfile
from cli_agent.session.store import SessionStore


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    values = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    values.update(extra)
    return values


def _open(tmp_path: Path) -> tuple[RuntimeBundle, str]:
    bundle = build_runtime(
        BuildOptions(
            provider="deepseek",
            model="deepseek-chat",
            persist=True,
            cwd=tmp_path,
            env=_env(tmp_path, DEEPSEEK_API_KEY="sk-test"),
        )
    )
    return bundle, bundle.session_id


async def test_resume_model_override_wins(tmp_path: Path) -> None:
    bundle, session_id = _open(tmp_path)
    await bundle.runtime.close()

    resumed = build_runtime(
        BuildOptions(
            resume_id=session_id,
            model="new-model",
            persist=True,
            cwd=tmp_path,
            env=_env(tmp_path, DEEPSEEK_API_KEY="sk-test"),
        )
    )
    try:
        assert resumed.profile.model == "new-model"
        assert resumed.profile.provider_name == "deepseek"
    finally:
        await resumed.runtime.close()


async def test_resume_provider_override_uses_new_provider_default(tmp_path: Path) -> None:
    bundle, session_id = _open(tmp_path)
    await bundle.runtime.close()

    resumed = build_runtime(
        BuildOptions(
            resume_id=session_id,
            provider="anthropic",
            persist=True,
            cwd=tmp_path,
            env=_env(tmp_path, ANTHROPIC_API_KEY="sk-test"),
        )
    )
    try:
        assert resumed.profile.provider_name == "anthropic"
        assert resumed.profile.model == "claude-sonnet-4-6"
    finally:
        await resumed.runtime.close()


async def test_resume_without_overrides_keeps_session_model(tmp_path: Path) -> None:
    bundle, session_id = _open(tmp_path)
    await bundle.runtime.close()

    resumed = build_runtime(
        BuildOptions(
            resume_id=session_id,
            persist=True,
            cwd=tmp_path,
            env=_env(tmp_path, DEEPSEEK_API_KEY="sk-test"),
        )
    )
    try:
        assert resumed.profile.model == "deepseek-chat"
        assert resumed.profile.provider_name == "deepseek"
    finally:
        await resumed.runtime.close()


async def test_failed_resume_releases_session_lock(tmp_path: Path) -> None:
    bundle, session_id = _open(tmp_path)
    await bundle.runtime.close()

    with pytest.raises(ConfigError):
        build_runtime(
            BuildOptions(
                resume_id=session_id,
                provider="missing-provider",
                persist=True,
                cwd=tmp_path,
                env=_env(tmp_path, DEEPSEEK_API_KEY="sk-test"),
            )
        )

    paths = AppPaths.discover(tmp_path, _env(tmp_path))
    resumed, loaded = SessionStore.resume(paths, session_id)
    try:
        assert loaded.messages == ()
    finally:
        resumed.close()


async def test_resume_missing_key_releases_lock(tmp_path: Path) -> None:
    bundle, session_id = _open(tmp_path)
    await bundle.runtime.close()

    with pytest.raises(ConfigError):
        build_runtime(
            BuildOptions(
                resume_id=session_id,
                provider="deepseek",
                persist=True,
                cwd=tmp_path,
                env=_env(tmp_path),
            )
        )

    paths = AppPaths.discover(tmp_path, _env(tmp_path))
    resumed, _loaded = SessionStore.resume(paths, session_id)
    resumed.close()


def test_failed_new_session_closes_provider(tmp_path: Path, monkeypatch) -> None:
    class FakeProvider:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    fake = FakeProvider()
    profile = ModelProfile("mock", "anthropic_compat", "mock", 1_000, 100)

    monkeypatch.setattr(
        assembly, "build_provider", lambda *args, **kwargs: (fake, profile)
    )

    def fail_new(cls, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(SessionStore, "new", classmethod(fail_new))

    with pytest.raises(OSError):
        build_runtime(
            BuildOptions(
                provider="deepseek",
                model="mock",
                persist=True,
                cwd=tmp_path,
                env=_env(tmp_path, DEEPSEEK_API_KEY="sk-test"),
            )
        )
    assert fake.closed
