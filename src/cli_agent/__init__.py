"""cli-agent: a small, provider-neutral CLI coding agent."""

from __future__ import annotations

__version__ = "0.1.0"

from .assembly import BuildOptions, RuntimeBundle, build_runtime  # noqa: E402
from .config import AppConfig, load_config  # noqa: E402
from .runtime import AgentRuntime  # noqa: E402

__all__ = [
    "AgentRuntime",
    "AppConfig",
    "BuildOptions",
    "RuntimeBundle",
    "build_runtime",
    "load_config",
    "__version__",
]
