"""XDG paths, TOML configuration, and provider presets."""

from __future__ import annotations

import hashlib
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import ConfigError

APP_NAME = "dcx"
LEGACY_APP_NAME = "cli-agent"
PERMISSION_MODES = ("bypass", "accept-edits", "default")
MIN_THINKING_BUDGET = 1_024
DEFAULT_THINKING_BUDGET = 4_096


def _xdg_home(env: Mapping[str, str], key: str, fallback: str) -> Path:
    value = env.get(key)
    return Path(value).expanduser() if value else Path.home() / fallback


def _app_directory(base: Path) -> Path:
    """Expose existing state at the new path without moving historical files.

    The alias keeps absolute spill paths and open session locks valid. An
    existing dcx directory always wins; independent stores are never merged.
    """
    current = base / APP_NAME
    legacy = base / LEGACY_APP_NAME
    if current.exists():
        if not current.is_dir():
            raise ConfigError(f"application path is not a directory: {current}")
        return current
    if current.is_symlink():
        raise ConfigError(f"application directory is a broken symlink: {current}")
    if legacy.is_dir():
        try:
            current.symlink_to(legacy.resolve(), target_is_directory=True)
        except FileExistsError:
            # Another process may discover the same directory concurrently.
            if not current.is_dir():
                raise ConfigError(f"application path is not a directory: {current}") from None
        except OSError as exc:
            raise ConfigError(
                f"could not link {current} to existing state at {legacy}: {exc}"
            ) from exc
    return current


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    cwd: Path
    project_id: str

    @classmethod
    def discover(cls, cwd: str | Path | None = None, env: Mapping[str, str] | None = None) -> AppPaths:
        env = os.environ if env is None else env
        working_dir = Path(cwd or Path.cwd()).expanduser().resolve()
        config_dir = _app_directory(_xdg_home(env, "XDG_CONFIG_HOME", ".config"))
        data_dir = _app_directory(_xdg_home(env, "XDG_DATA_HOME", ".local/share"))
        project_id = hashlib.sha256(str(working_dir).encode("utf-8")).hexdigest()[:12]
        return cls(config_dir=config_dir, data_dir=data_dir, cwd=working_dir, project_id=project_id)

    @property
    def project_dir(self) -> Path:
        return self.data_dir / "projects" / self.project_id

    @property
    def sessions_dir(self) -> Path:
        return self.project_dir / "sessions"

    @property
    def memory_dir(self) -> Path:
        return self.project_dir / "memory"

    @property
    def global_skills_dir(self) -> Path:
        return self.config_dir / "skills"

    @property
    def project_skills_dir(self) -> Path:
        return self.cwd / ".agents" / "skills"

    @property
    def global_agents_md(self) -> Path:
        return self.config_dir / "AGENTS.md"

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / session_id


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    kind: str = "anthropic_compat"
    base_url: str | None = None
    api_key_env: tuple[str, ...] = ()
    default_model: str = ""
    context_window: int = 200_000
    max_output_tokens: int = 16_384
    supports_images: bool = False
    supports_thinking: bool = False
    thinking_by_default: bool = False
    replay_reasoning: bool = False
    prompt_caching: bool = False


@dataclass(frozen=True, slots=True)
class ModelConfig:
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_images: bool | None = None
    supports_thinking: bool | None = None
    replay_reasoning: bool | None = None
    prompt_caching: bool | None = None


@dataclass(frozen=True, slots=True)
class RuleConfig:
    tool: str
    args: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IngestConfig:
    """System-side document ingestion settings (never agent runtime)."""

    basic_dpi: int = 72
    max_upload_mb: int = 200
    max_files: int = 50
    marker_command: str = "marker_single"
    marker_timeout: int = 1800


@dataclass(frozen=True, slots=True)
class AppConfig:
    paths: AppPaths
    default_provider: str
    default_model: str | None
    permission_mode: str
    max_turns: int
    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)
    models: Mapping[str, ModelConfig] = field(default_factory=dict)
    allow_rules: tuple[RuleConfig, ...] = ()
    deny_rules: tuple[RuleConfig, ...] = ()
    memory_enabled: bool = True
    thinking_enabled: bool | None = None
    thinking_budget: int = DEFAULT_THINKING_BUDGET
    mcp_servers: Mapping[str, Any] = field(default_factory=dict)
    document_libraries: tuple[str, ...] = ()
    default_document_library: str | None = None
    ingest: IngestConfig = field(default_factory=IngestConfig)

    def provider(self, name: str | None = None) -> ProviderConfig:
        key = name or self.default_provider
        try:
            return self.providers[key]
        except KeyError as exc:
            available = ", ".join(sorted(self.providers))
            raise ConfigError(f"unknown provider {key!r}; available: {available}") from exc

    def thinking_for(self, provider_name: str | None = None) -> bool:
        """Effective thinking request setting for a provider.

        ``None`` means "use the provider default"; an explicit config or CLI
        value wins.
        """
        provider = self.provider(provider_name)
        if self.thinking_enabled is not None:
            return self.thinking_enabled
        return provider.thinking_by_default

    def with_overrides(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        permission_mode: str | None = None,
        max_turns: int | None = None,
        memory_enabled: bool | None = None,
        thinking_enabled: bool | None = None,
        thinking_budget: int | None = None,
    ) -> AppConfig:
        if permission_mode is not None and permission_mode not in PERMISSION_MODES:
            raise ConfigError(
                f"invalid permission mode {permission_mode!r}; "
                f"choose one of {', '.join(PERMISSION_MODES)}"
            )
        return replace(
            self,
            default_provider=provider or self.default_provider,
            default_model=model if model is not None else self.default_model,
            permission_mode=permission_mode or self.permission_mode,
            max_turns=max_turns if max_turns is not None else self.max_turns,
            memory_enabled=memory_enabled if memory_enabled is not None else self.memory_enabled,
            thinking_enabled=(
                thinking_enabled
                if thinking_enabled is not None
                else self.thinking_enabled
            ),
            thinking_budget=(
                thinking_budget
                if thinking_budget is not None
                else self.thinking_budget
            ),
        )


BUILTIN_PROVIDERS: dict[str, ProviderConfig] = {
    "anthropic": ProviderConfig(
        name="anthropic",
        base_url=None,
        api_key_env=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        default_model="claude-sonnet-4-6",
        context_window=256_000,
        max_output_tokens=128_000,
        supports_images=True,
        supports_thinking=True,
        prompt_caching=True,
    ),
    "deepseek": ProviderConfig(
        name="deepseek",
        base_url="https://api.deepseek.com/anthropic",
        api_key_env=("DEEPSEEK_API_KEY",),
        default_model="deepseek-flash[1M]",
        context_window=1_000_000,
        max_output_tokens=128_000,
        supports_images=True,
        # Verified against DeepSeek's Anthropic-compatible endpoint.
        supports_thinking=True,
        thinking_by_default=True,
    ),
    "kimi": ProviderConfig(
        name="kimi",
        base_url="https://api.moonshot.cn/anthropic",
        api_key_env=("MOONSHOT_API_KEY",),
        default_model="kimi-k2.7-code",
        context_window=512_000,
        max_output_tokens=128_000,
    ),
    "minimax": ProviderConfig(
        name="minimax",
        base_url="https://api.minimax.com/anthropic",
        api_key_env=("MINIMAX_API_KEY",),
        default_model="MiniMax-M3",
        context_window=512_000,
        max_output_tokens=128_000,
    ),
    "glm": ProviderConfig(
        name="glm",
        base_url="https://open.bigmodel.cn/api/anthropic",
        api_key_env=("GLM_API_KEY", "ZHIPU_API_KEY"),
        default_model="glm-5.2",
        context_window=512_000,
        max_output_tokens=128_000,
    ),
}


def load_config(
    config_path: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    env = os.environ if env is None else env
    paths = AppPaths.discover(cwd, env)
    data: dict[str, Any] = {}
    path = Path(config_path).expanduser() if config_path else paths.config_dir / "config.toml"
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"could not read config {path}: {exc}") from exc
    elif config_path is not None:
        raise ConfigError(f"config file not found: {path}")

    providers = dict(BUILTIN_PROVIDERS)
    raw_providers = data.get("providers", {})
    if not isinstance(raw_providers, dict):
        raise ConfigError("[providers] must be a table")
    for name, raw in raw_providers.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"[providers.{name}] must be a table")
        base = providers.get(name, ProviderConfig(name=name))
        providers[name] = _merge_provider(base, raw)

    # Per-model settings come only from the user's config; provider presets own
    # the defaults. A builtin model table here would silently shadow explicit
    # [providers.*] values (see build_profile).
    models: dict[str, ModelConfig] = {}
    raw_models = data.get("models", {})
    if not isinstance(raw_models, dict):
        raise ConfigError("[models] must be a table")
    for name, raw in raw_models.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"[models.{name}] must be a table")
        models[name] = _merge_model(models.get(name), raw)

    default_provider = str(data.get("default_provider", "anthropic"))
    if default_provider not in providers:
        raise ConfigError(f"default_provider {default_provider!r} is not configured")
    default_model = data.get("default_model")
    if default_model is not None:
        default_model = str(default_model)

    permission_mode = str(data.get("permission_mode", "accept-edits"))
    if permission_mode not in PERMISSION_MODES:
        raise ConfigError(
            f"invalid permission_mode {permission_mode!r}; "
            f"choose one of {', '.join(PERMISSION_MODES)}"
        )

    max_turns = _optional_int(data, "max_turns")
    if max_turns is None:
        max_turns = 50
    if max_turns < 1:
        raise ConfigError("max_turns must be at least 1")
    memory_enabled = _parse_memory(data)
    allow_rules, deny_rules = _parse_permission_rules(data.get("permissions", {}))
    thinking_enabled, thinking_budget = _parse_thinking(data.get("thinking", {}))
    mcp_servers = data.get("mcp_servers", {})
    if not isinstance(mcp_servers, dict):
        raise ConfigError("mcp_servers must be a table")
    deepdocx = data.get("deepdocx", {})
    if not isinstance(deepdocx, dict) or not isinstance(deepdocx.get("libraries", []), list):
        raise ConfigError("deepdocx.libraries must be an array of directory paths")
    default_document_library = deepdocx.get("default_library")
    if default_document_library is not None and not isinstance(default_document_library, str):
        raise ConfigError("deepdocx.default_library must be a directory path")
    ingest = _parse_ingest(deepdocx.get("ingest", {}))

    return AppConfig(
        paths=paths,
        default_provider=default_provider,
        default_model=default_model,
        permission_mode=permission_mode,
        max_turns=max_turns,
        providers=providers,
        models=models,
        allow_rules=allow_rules,
        deny_rules=deny_rules,
        memory_enabled=memory_enabled,
        thinking_enabled=thinking_enabled,
        thinking_budget=thinking_budget,
        mcp_servers=mcp_servers,
        document_libraries=tuple(str(path) for path in deepdocx.get("libraries", [])),
        default_document_library=default_document_library,
        ingest=ingest,
    )


def _parse_ingest(raw: object) -> IngestConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("deepdocx.ingest must be a table")
    basic_dpi = _optional_int(raw, "basic_dpi")
    max_upload_mb = _optional_int(raw, "max_upload_mb")
    max_files = _optional_int(raw, "max_files")
    marker_timeout = _optional_int(raw, "marker_timeout")
    marker_command = raw.get("marker_command", "marker_single")
    if not isinstance(marker_command, str) or not marker_command.strip():
        raise ConfigError("deepdocx.ingest.marker_command must be a non-empty string")
    settings = IngestConfig(
        basic_dpi=72 if basic_dpi is None else basic_dpi,
        max_upload_mb=200 if max_upload_mb is None else max_upload_mb,
        max_files=50 if max_files is None else max_files,
        marker_command=marker_command,
        marker_timeout=1800 if marker_timeout is None else marker_timeout,
    )
    if not 36 <= settings.basic_dpi <= 600:
        raise ConfigError("deepdocx.ingest.basic_dpi must be between 36 and 600")
    if not 1 <= settings.max_upload_mb <= 2048:
        raise ConfigError("deepdocx.ingest.max_upload_mb must be between 1 and 2048")
    if not 1 <= settings.max_files <= 200:
        raise ConfigError("deepdocx.ingest.max_files must be between 1 and 200")
    if settings.marker_timeout < 30:
        raise ConfigError("deepdocx.ingest.marker_timeout must be at least 30 seconds")
    return settings


def _merge_provider(base: ProviderConfig, raw: Mapping[str, Any]) -> ProviderConfig:
    kind = str(raw.get("kind", base.kind))
    if kind not in ("anthropic_compat", "openai_compat"):
        raise ConfigError(f"unsupported provider kind {kind!r}")
    if kind == "openai_compat":
        raise ConfigError("openai_compat is reserved for a later release; use anthropic_compat")

    api_key_env = raw.get("api_key_env", base.api_key_env)
    if isinstance(api_key_env, str):
        api_key_env = (api_key_env,)
    elif isinstance(api_key_env, (list, tuple)):
        api_key_env = tuple(str(item) for item in api_key_env)
    elif api_key_env is not None:
        raise ConfigError(f"providers.{base.name}.api_key_env must be a string or list")

    base_url = raw.get("base_url", base.base_url)
    max_output_tokens = _optional_int(raw, "max_output_tokens")
    return ProviderConfig(
        name=base.name,
        kind=kind,
        base_url=str(base_url) if base_url is not None else None,
        api_key_env=api_key_env or (),
        default_model=str(raw.get("default_model", base.default_model)),
        context_window=_optional_int(raw, "context_window") or base.context_window,
        max_output_tokens=(
            max_output_tokens
            if max_output_tokens is not None
            else base.max_output_tokens
        ),
        supports_images=bool(raw.get("supports_images", base.supports_images)),
        supports_thinking=bool(raw.get("supports_thinking", base.supports_thinking)),
        thinking_by_default=bool(
            raw.get("thinking_by_default", base.thinking_by_default)
        ),
        replay_reasoning=bool(raw.get("replay_reasoning", base.replay_reasoning)),
        prompt_caching=bool(raw.get("prompt_caching", base.prompt_caching)),
    )


def _optional_int(raw: Mapping[str, Any], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer")
    return value


def _optional_bool(raw: Mapping[str, Any], key: str) -> bool | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean")
    return value


def _parse_memory(data: Mapping[str, Any]) -> bool:
    """Memory is opt-out. Legacy extraction knobs are accepted and ignored."""
    raw = data.get("memory", {})
    if not isinstance(raw, dict):
        raise ConfigError("[memory] must be a table")
    enabled = _optional_bool(raw, "enabled")
    if enabled is None:
        enabled = _optional_bool(data, "memory_enabled")
    if enabled is None:
        enabled = True
    return enabled


def _merge_model(base: ModelConfig | None, raw: Mapping[str, Any]) -> ModelConfig:
    return ModelConfig(
        context_window=(
            _optional_int(raw, "context_window")
            if "context_window" in raw
            else (base.context_window if base else None)
        ),
        max_output_tokens=(
            _optional_int(raw, "max_output_tokens")
            if "max_output_tokens" in raw
            else (base.max_output_tokens if base else None)
        ),
        supports_images=(
            _optional_bool(raw, "supports_images")
            if "supports_images" in raw
            else (base.supports_images if base else None)
        ),
        supports_thinking=(
            _optional_bool(raw, "supports_thinking")
            if "supports_thinking" in raw
            else (base.supports_thinking if base else None)
        ),
        replay_reasoning=(
            _optional_bool(raw, "replay_reasoning")
            if "replay_reasoning" in raw
            else (base.replay_reasoning if base else None)
        ),
        prompt_caching=(
            _optional_bool(raw, "prompt_caching")
            if "prompt_caching" in raw
            else (base.prompt_caching if base else None)
        ),
    )


def _parse_thinking(raw: Any) -> tuple[bool | None, int]:
    if not isinstance(raw, dict):
        raise ConfigError("[thinking] must be a table")
    enabled = _optional_bool(raw, "enabled")
    budget = _optional_int(raw, "budget_tokens")
    if budget is None:
        budget = DEFAULT_THINKING_BUDGET
    if budget < MIN_THINKING_BUDGET:
        raise ConfigError(
            f"thinking.budget_tokens must be at least {MIN_THINKING_BUDGET}"
        )
    return (enabled, budget)


def _parse_permission_rules(raw: Any) -> tuple[tuple[RuleConfig, ...], tuple[RuleConfig, ...]]:
    if not isinstance(raw, dict):
        raise ConfigError("[permissions] must be a table")
    return (
        _parse_rule_list(raw.get("allow", []), "allow"),
        _parse_rule_list(raw.get("deny", []), "deny"),
    )


def _parse_rule_list(raw: Any, kind: str) -> tuple[RuleConfig, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"permissions.{kind} must be an array of tables")
    rules: list[RuleConfig] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"permissions.{kind}[{index}] must be a table")
        tool = item.get("tool")
        if not isinstance(tool, str) or not tool:
            raise ConfigError(f"permissions.{kind}[{index}].tool must be a non-empty string")
        raw_args = item.get("args", {})
        if not isinstance(raw_args, dict):
            raise ConfigError(f"permissions.{kind}[{index}].args must be a table")
        args = {str(key): str(value) for key, value in raw_args.items()}
        rules.append(RuleConfig(tool=tool, args=args))
    return tuple(rules)


def resolve_api_key(provider: ProviderConfig, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    for key_env in provider.api_key_env:
        value = env.get(key_env)
        if value:
            return value
    names = ", ".join(provider.api_key_env) or "(none configured)"
    raise ConfigError(
        f"no API key for provider {provider.name!r}; set one of: {names}"
    )


def resolve_base_url(provider: ProviderConfig, env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    env_name = f"{provider.name.upper().replace('-', '_')}_BASE_URL"
    if env.get(env_name):
        return env[env_name]
    if provider.name == "anthropic" and env.get("ANTHROPIC_BASE_URL"):
        return env["ANTHROPIC_BASE_URL"]
    return provider.base_url
