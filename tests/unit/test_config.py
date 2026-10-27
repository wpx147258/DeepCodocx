from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent.config import (
    AppPaths,
    load_config,
    resolve_api_key,
    resolve_base_url,
)
from cli_agent.errors import ConfigError
from cli_agent.providers.factory import build_profile


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }


def test_defaults_are_generic_and_use_xdg(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    assert config.default_provider == "anthropic"
    assert config.permission_mode == "accept-edits"
    assert config.max_turns == 50
    assert "deepseek" in config.providers
    assert config.paths.config_dir == tmp_path / "config" / "cli-agent"
    assert config.paths.data_dir == tmp_path / "data" / "cli-agent"
    assert len(config.paths.project_id) == 12


def test_custom_provider_model_rules(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
default_provider = "local"
default_model = "local-model"
permission_mode = "default"
max_turns = 7

[providers.local]
kind = "anthropic_compat"
base_url = "http://127.0.0.1:1234/anthropic"
api_key_env = ["LOCAL_KEY"]
default_model = "local-model"
context_window = 32000
max_output_tokens = 4096

[models."local-model"]
context_window = 64000

[[permissions.deny]]
tool = "Bash"
args = { command = "rm -rf *" }

[[permissions.allow]]
tool = "Read"
""",
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    assert config.default_provider == "local"
    assert config.default_model == "local-model"
    assert config.permission_mode == "default"
    assert config.max_turns == 7
    assert config.providers["local"].base_url == "http://127.0.0.1:1234/anthropic"
    assert config.models["local-model"].context_window == 64000
    assert config.deny_rules[0].args["command"] == "rm -rf *"
    assert config.allow_rules[0].tool == "Read"


def test_explicit_missing_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml", cwd=tmp_path, env=_env(tmp_path))


def test_invalid_permission_mode(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text('permission_mode = "yolo"', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(cwd=tmp_path, env=_env(tmp_path))


def test_overrides_validate_provider_later(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    with pytest.raises(ConfigError):
        config.with_overrides(provider="missing").provider()


def test_api_key_lookup_does_not_cross_providers(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    deepseek = config.providers["deepseek"]
    with pytest.raises(ConfigError):
        resolve_api_key(deepseek, {"ANTHROPIC_API_KEY": "sk-ant"})
    assert resolve_api_key(deepseek, {"DEEPSEEK_API_KEY": "sk-ds"}) == "sk-ds"


def test_base_url_env_override(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    deepseek = config.providers["deepseek"]
    assert (
        resolve_base_url(deepseek, {"DEEPSEEK_BASE_URL": "http://proxy"})
        == "http://proxy"
    )


def test_paths_are_project_scoped(tmp_path: Path) -> None:
    paths = AppPaths.discover(tmp_path, _env(tmp_path))
    assert paths.project_dir == paths.data_dir / "projects" / paths.project_id
    assert paths.session_dir("sess_abc") == paths.sessions_dir / "sess_abc"


def test_thinking_config_and_capabilities(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[thinking]
enabled = true
budget_tokens = 4096
""",
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    assert config.thinking_enabled is True
    assert config.thinking_budget == 4096
    assert config.providers["anthropic"].supports_thinking is True
    assert config.providers["deepseek"].supports_thinking is True
    assert config.providers["kimi"].supports_thinking is False


def test_thinking_budget_floor(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        "[thinking]\nbudget_tokens = 100\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="at least 1024"):
        load_config(cwd=tmp_path, env=_env(tmp_path))


def test_thinking_overrides(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    updated = config.with_overrides(thinking_enabled=True, thinking_budget=2048)
    assert updated.thinking_enabled is True
    assert updated.thinking_budget == 2048


def test_model_can_opt_into_thinking(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[models."custom-model"]\nsupports_thinking = true\n',
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    assert config.models["custom-model"].supports_thinking is True


def test_deepseek_defaults_to_flash_and_thinking(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    deepseek = config.providers["deepseek"]
    assert deepseek.default_model == "deepseek-flash[1M]"
    assert deepseek.thinking_by_default is True
    assert config.thinking_enabled is None
    assert config.thinking_for("deepseek") is True
    assert config.thinking_for("anthropic") is False
    assert "deepseek-flash[1M]" not in config.models


def test_explicit_thinking_false_overrides_provider_default(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        "[thinking]\nenabled = false\n", encoding="utf-8"
    )
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    assert config.thinking_for("deepseek") is False


def test_model_override_holds_only_explicit_fields(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[models."deepseek-flash[1M]"]\nmax_output_tokens = 4096\n',
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    model = config.models["deepseek-flash[1M]"]
    assert model.context_window is None
    assert model.max_output_tokens == 4096


def test_memory_enabled_settings(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("[memory]\nenabled = false\n", encoding="utf-8")
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    assert config.memory_enabled is False


def test_memory_is_enabled_by_default(tmp_path: Path) -> None:
    assert load_config(cwd=tmp_path, env=_env(tmp_path)).memory_enabled is True


def test_legacy_extraction_knobs_are_ignored(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        "[memory]\nenabled = true\nextraction_min_new_messages = 12\n",
        encoding="utf-8",
    )
    assert load_config(cwd=tmp_path, env=_env(tmp_path)).memory_enabled is True


def test_prompt_caching_defaults_and_overrides(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    # The first-party preset caches the static prompt prefix; compatible
    # third-party endpoints must not receive cache_control unless opted in.
    assert config.providers["anthropic"].prompt_caching is True
    assert config.providers["deepseek"].prompt_caching is False
    profile = build_profile(config, config.providers["anthropic"], "claude-sonnet-4-6")
    assert profile.prompt_caching is True

    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        """
[providers.deepseek]
prompt_caching = true

[models."deepseek-flash[1M]"]
prompt_caching = false
""",
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    assert config.providers["deepseek"].prompt_caching is True
    assert config.models["deepseek-flash[1M]"].prompt_caching is False
    profile = build_profile(config, config.providers["deepseek"], "deepseek-flash[1M]")
    assert profile.prompt_caching is False


def test_image_capability_defaults_and_overrides(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    assert config.providers["anthropic"].supports_images is True
    assert config.providers["deepseek"].supports_images is True
    profile = build_profile(config, config.providers["deepseek"], "deepseek-flash[1M]")
    assert profile.supports_images is True

    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        '[models."text-only"]\nsupports_images = false\n', encoding="utf-8"
    )
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    profile = build_profile(config, config.providers["deepseek"], "text-only")
    assert profile.supports_images is False


def test_max_turns_zero_is_rejected(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("max_turns = 0", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_turns"):
        load_config(cwd=tmp_path, env=_env(tmp_path))


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_max_output_tokens_is_rejected(
    tmp_path: Path, value: int
) -> None:
    config_dir = tmp_path / "config" / "cli-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        f"[providers.deepseek]\nmax_output_tokens = {value}\n", encoding="utf-8"
    )
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    with pytest.raises(ConfigError, match="max_output_tokens"):
        build_profile(config, config.providers["deepseek"], "deepseek-chat")
