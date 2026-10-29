from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import dcx.assembly as assembly
from dcx.assembly import BuildOptions, build_runtime
from dcx.providers.base import ModelProfile
from tests.support.fake_provider import FakeProvider


def _install_fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider([])
    profile = ModelProfile(
        provider_name="deepseek",
        provider_kind="anthropic_compat",
        model="m1",
        context_window=200_000,
        max_output_tokens=8_192,
    )

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


async def test_auto_mode_survives_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_provider(monkeypatch)
    bundle = build_runtime(_options(tmp_path))
    runtime = bundle.runtime
    assert runtime.permission_mode == "accept-edits"
    assert runtime.set_auto(True) == "bypass"
    session_id = bundle.session_id
    await runtime.close()

    resumed = build_runtime(_options(tmp_path, resume_id=session_id))
    try:
        assert resumed.runtime.permission_mode == "bypass"
        # The mode replaced by /auto is remembered across the resume.
        assert resumed.runtime.set_auto(False) == "accept-edits"
        assert resumed.runtime.permission_mode == "accept-edits"
    finally:
        await resumed.runtime.close()


async def test_explicit_permission_mode_beats_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_provider(monkeypatch)
    bundle = build_runtime(_options(tmp_path))
    bundle.runtime.set_auto(True)
    session_id = bundle.session_id
    await bundle.runtime.close()

    resumed = build_runtime(
        _options(tmp_path, resume_id=session_id, permission_mode="default")
    )
    try:
        assert resumed.runtime.permission_mode == "default"
    finally:
        await resumed.runtime.close()
