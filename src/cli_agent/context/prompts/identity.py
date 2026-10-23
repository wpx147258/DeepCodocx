"""Who the agent is and which model is driving it."""

from __future__ import annotations

from ...providers.base import SystemSection
from .types import PromptContext


def build(context: PromptContext) -> SystemSection:
    text = f"""You are {context.agent_name}, an AI coding agent running in a terminal on the user's project.
You are powered by the {context.profile.provider_name} provider using the
{context.profile.model} model.

Act on the request rather than describing what you would do. Verify your work when
practical, and keep responses concise. You are not a chatbot or an oracle: inspect the
environment with tools before making claims about it."""
    return SystemSection("identity", text, cacheable=True)
