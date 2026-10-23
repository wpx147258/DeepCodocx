from __future__ import annotations

import pytest

from cli_agent.assembly import _complete_request
from cli_agent.errors import ProviderFailure
from cli_agent.messages import user_message
from cli_agent.providers.base import ModelRequest
from tests.support.fake_provider import FakeProvider, fail, text_response


def _request() -> ModelRequest:
    return ModelRequest(
        model="mock",
        system=(),
        messages=(user_message("hi"),),
        tools=(),
        max_output_tokens=100,
    )


async def test_retryable_helper_error_is_retried() -> None:
    provider = FakeProvider(
        [[fail("rate limited", "retryable")], [text_response("ok")]]
    )
    result = await _complete_request(provider, _request(), base_delay=0)
    assert result == "ok"
    assert len(provider.requests) == 2


async def test_non_retryable_helper_error_fails_fast() -> None:
    provider = FakeProvider([[fail("bad request", "invalid_request")]])
    with pytest.raises(ProviderFailure):
        await _complete_request(provider, _request(), base_delay=0)
    assert len(provider.requests) == 1
