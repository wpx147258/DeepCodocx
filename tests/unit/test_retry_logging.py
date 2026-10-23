from __future__ import annotations

import logging

import anthropic
import httpx2

from cli_agent.assembly import _complete_request
from cli_agent.messages import user_message
from cli_agent.providers.anthropic_compat import AnthropicCompatProvider
from cli_agent.providers.base import ModelProfile, ModelRequest, ProviderError
from tests.support.fake_provider import FakeProvider, fail, text_response


def _request() -> ModelRequest:
    return ModelRequest(
        model="mock",
        system=(),
        messages=(user_message("hi"),),
        tools=(),
        max_output_tokens=100,
        profile=ModelProfile("mock", "anthropic_compat", "mock", 1000, 100),
    )


class _RaisingStream:
    async def __aenter__(self):
        raise anthropic.APIConnectionError(
            request=httpx2.Request("GET", "http://example.com")
        )

    async def __aexit__(self, *args) -> None:
        return None


class _RaisingMessages:
    def stream(self, **params):
        return _RaisingStream()


class _RaisingClient:
    def __init__(self) -> None:
        self.messages = _RaisingMessages()

    async def close(self) -> None:
        return None


async def test_retryable_adapter_error_logs_at_info_not_warning(caplog) -> None:
    provider = AnthropicCompatProvider(api_key="test", client=_RaisingClient())
    with caplog.at_level(logging.INFO):
        collected = [event async for event in provider.stream(_request())]
    assert isinstance(collected[-1], ProviderError)
    assert collected[-1].kind == "retryable"
    assert "provider error (retryable)" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


async def test_helper_retry_logs_at_info_not_warning(caplog) -> None:
    provider = FakeProvider(
        [[fail("rate limited", "retryable")], [text_response("ok")]]
    )
    with caplog.at_level(logging.INFO):
        result = await _complete_request(provider, _request(), base_delay=0)
    assert result == "ok"
    assert "helper provider call failed" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
