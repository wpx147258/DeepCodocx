"""Serializable service configuration, never credentials or agent state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

MODEL = "Qwen3-VL-Embedding-8B"


@dataclass(frozen=True)
class IndexConfig:
    enabled: bool = True
    auto_sync: bool = False
    base_url: str = "https://ai.gitee.com/v1"
    api_key_env: str = "GITEE_API_KEY"
    model: str = MODEL
    dimensions: int = 1024
    batch_size: int = 8
    timeout: int = 60
    block_chars: int = 1800
    joint_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    joint_key_env: str = "DASHSCOPE_API_KEY"
    joint_protocol: str = "dashscope"
    joint_model: str = "qwen3-vl-embedding"
    joint_dimensions: int = 1024
    revision: str = "qwen3-vl-v1"

    @property
    def embedding_profile(self) -> str:
        values = {
            key: getattr(self, key) for key in ("base_url", "model", "dimensions", "revision")
        }
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:24]

    @property
    def joint_profile(self) -> str:
        values = {
            key: getattr(self, key)
            for key in (
                "joint_base_url",
                "joint_model",
                "joint_protocol",
                "joint_dimensions",
                "revision",
            )
        }
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:24]

    @property
    def profile(self) -> str:
        values = {
            key: value
            for key, value in asdict(self).items()
            if key
            not in {"enabled", "auto_sync", "batch_size", "timeout", "api_key_env", "joint_key_env"}
        }
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class CaptionConfig:
    enabled: bool = True
    base_url: str = "https://ai.gitee.com/v1"
    api_key_env: str = "GITEE_API_KEY"
    model: str = "qwen3.8-flash"
    timeout: int = 90
    max_tokens: int = 800
    revision: str = "scientific-description-v1"

    @property
    def profile(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:24]


def _url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "service base_url must be an HTTP(S) URL without credentials, query or fragment"
        )
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("remote inference services require HTTPS")


def parse_settings(index: Any, caption: Any) -> tuple[IndexConfig, CaptionConfig]:
    parsed = []
    for cls, raw in ((IndexConfig, index), (CaptionConfig, caption)):
        if not isinstance(raw, dict):
            raise ValueError(f"deepdocx.{cls.__name__} must be a table")
        defaults = asdict(cls())
        unknown = set(raw) - defaults.keys()
        if unknown:
            raise ValueError(f"Unknown index/caption settings: {', '.join(sorted(unknown))}")
        for key, value in raw.items():
            expected = type(defaults[key])
            if expected is float:
                valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            else:
                valid = type(value) is expected
            if not valid:
                raise ValueError(f"Invalid type for deepdocx index/caption {key}")
        item = cls(**raw)
        _url(item.base_url)
        if not item.api_key_env or not item.model or not item.revision:
            raise ValueError("model, api_key_env and revision must be nonempty")
        if not 1 <= item.timeout <= 300:
            raise ValueError("service timeout must be between 1 and 300 seconds")
        parsed.append(item)
    idx, cap = parsed
    assert isinstance(idx, IndexConfig) and isinstance(cap, CaptionConfig)
    if idx.model != MODEL:
        raise ValueError(f"All embedding representations must use {MODEL}")
    if (
        not 64 <= idx.dimensions <= 4096
        or not 1 <= idx.batch_size <= 64
        or not 300 <= idx.block_chars <= 6000
    ):
        raise ValueError(
            "Invalid dimensions (64–4096), batch_size (1–64), or block_chars (300–6000)"
        )
    if idx.joint_base_url:
        _url(idx.joint_base_url)
    if idx.joint_protocol not in {"dashscope", "openai"}:
        raise ValueError("joint_protocol must be dashscope or openai")
    if idx.joint_protocol == "dashscope" and (
        idx.joint_model != "qwen3-vl-embedding"
        or idx.joint_dimensions not in {256, 512, 768, 1024, 1536, 2048, 2560}
    ):
        raise ValueError(
            "DashScope native fusion requires qwen3-vl-embedding and a supported joint_dimensions value"
        )
    if idx.joint_protocol == "openai" and (
        idx.joint_model != MODEL or not 64 <= idx.joint_dimensions <= 4096
    ):
        raise ValueError(
            "Native OpenAI endpoint must serve Qwen3-VL-Embedding-8B with 64–4096 dimensions"
        )
    if not idx.joint_key_env:
        raise ValueError("joint_key_env must be nonempty")
    if not 100 <= cap.max_tokens <= 3000:
        raise ValueError("caption.max_tokens must be between 100 and 3000")
    return idx, cap
