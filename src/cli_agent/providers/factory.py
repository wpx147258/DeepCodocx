"""Construct a provider and its model profile from explicit configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace

from ..config import (
    MIN_THINKING_BUDGET,
    AppConfig,
    ProviderConfig,
    resolve_api_key,
    resolve_base_url,
)
from ..errors import ConfigError
from .anthropic_compat import AnthropicCompatProvider
from .base import ModelProfile, Provider


def build_provider(
    config: AppConfig,
    *,
    provider_name: str | None = None,
    model: str | None = None,
    thinking_budget: int | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Provider, ModelProfile]:
    env = os.environ if env is None else env
    provider_config = config.provider(provider_name)
    model_name = model or config.default_model or provider_config.default_model
    if not model_name:
        raise ConfigError(
            f"no model selected for provider {provider_config.name!r}; "
            "pass --model or set default_model"
        )
    profile = build_profile(config, provider_config, model_name)
    if thinking_budget is not None:
        profile = _apply_thinking(profile, thinking_budget)
    if provider_config.kind != "anthropic_compat":
        raise ConfigError(
            f"provider kind {provider_config.kind!r} is not implemented in this release"
        )
    api_key = resolve_api_key(provider_config, env)
    base_url = resolve_base_url(provider_config, env)
    provider = AnthropicCompatProvider(api_key=api_key, base_url=base_url)
    return provider, profile


def _apply_thinking(profile: ModelProfile, budget: int) -> ModelProfile:
    if not profile.supports_thinking:
        raise ConfigError(
            f"provider {profile.provider_name!r} does not support extended thinking "
            "(set supports_thinking = true on the provider or model to opt in)"
        )
    if budget < MIN_THINKING_BUDGET:
        raise ConfigError(
            f"thinking budget must be at least {MIN_THINKING_BUDGET} tokens"
        )
    if budget >= profile.max_output_tokens:
        raise ConfigError(
            "thinking budget must be smaller than max_output_tokens "
            f"({budget} >= {profile.max_output_tokens})"
        )
    # Anthropic extended thinking requires the signed reasoning blocks to be
    # replayed when the conversation continues after a tool call.
    return replace(profile, replay_reasoning=True)


def build_profile(
    config: AppConfig,
    provider_config: ProviderConfig,
    model: str,
) -> ModelProfile:
    override = config.models.get(model)
    return ModelProfile(
        provider_name=provider_config.name,
        provider_kind="anthropic_compat",
        model=model,
        context_window=(
            override.context_window
            if override and override.context_window is not None
            else provider_config.context_window
        ),
        max_output_tokens=(
            override.max_output_tokens
            if override and override.max_output_tokens is not None
            else provider_config.max_output_tokens
        ),
        supports_images=(
            override.supports_images
            if override and override.supports_images is not None
            else provider_config.supports_images
        ),
        supports_thinking=(
            override.supports_thinking
            if override and override.supports_thinking is not None
            else provider_config.supports_thinking
        ),
        replay_reasoning=(
            override.replay_reasoning
            if override and override.replay_reasoning is not None
            else provider_config.replay_reasoning
        ),
        prompt_caching=(
            override.prompt_caching
            if override and override.prompt_caching is not None
            else provider_config.prompt_caching
        ),
    )
