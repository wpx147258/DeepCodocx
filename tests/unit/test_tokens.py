from __future__ import annotations

from cli_agent.context.tokens import TokenEstimator, prompt_tokens
from cli_agent.messages import user_message
from cli_agent.providers.base import SystemSection
from cli_agent.tools.base import ToolSpec

_TOOL = ToolSpec(
    name="Bash",
    description="Run a shell command in the workspace. " * 20,
    input_schema={
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
        "additionalProperties": False,
    },
)


def test_estimate_request_counts_system_and_tools() -> None:
    estimator = TokenEstimator()
    messages = (user_message("hello"),)
    messages_only = estimator.estimate_messages(messages)
    section = SystemSection("identity", "x" * 4_000)

    with_overhead = estimator.estimate_request(messages, (section,), (_TOOL,))
    assert with_overhead > messages_only + 1_000


def test_calibration_stays_near_one_when_overhead_is_included() -> None:
    estimator = TokenEstimator()
    messages = (user_message("hello"),)
    system = (SystemSection("identity", "prompt text " * 500),)
    tools = (_TOOL,)
    for _ in range(40):
        estimate = estimator.estimate_request(messages, system, tools)
        estimator.calibrate(estimate, int(estimate * 1.05))
    # With the constant system/tool overhead inside the estimate, the observed
    # ratio tracks the estimator instead of pinning the factor at 2.0.
    assert estimator.factor < 1.2


def test_calibration_ignores_empty_inputs() -> None:
    estimator = TokenEstimator()
    estimator.calibrate(0, 100)
    estimator.calibrate(100, 0)
    assert estimator.factor == 1.0


def test_prompt_tokens_includes_cached_prefix() -> None:
    from cli_agent.messages import Usage

    usage = Usage(
        input_tokens=331,
        output_tokens=921,
        cache_read_tokens=299_008,
        cache_write_tokens=1_024,
    )
    # Prompt size is input + cached prefix; output tokens are not part of it.
    assert prompt_tokens(usage) == 331 + 299_008 + 1_024
