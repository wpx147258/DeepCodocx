from __future__ import annotations

from pathlib import Path

from cli_agent.assembly import BuildOptions, RuntimeBundle, build_runtime


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
