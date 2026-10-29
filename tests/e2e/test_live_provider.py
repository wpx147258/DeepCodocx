"""Live provider smoke tests.

Skipped unless DCX_E2E_PROVIDER is set. They never fall back to another
provider: configure that provider's key env before running.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dcx.assembly import BuildOptions, build_runtime
from dcx.events import AgentTextDelta, AgentToolFinished

pytestmark = pytest.mark.skipif(
    not os.environ.get("DCX_E2E_PROVIDER"),
    reason="set DCX_E2E_PROVIDER to run live provider tests",
)


def _bundle(tmp_path: Path):
    provider = os.environ["DCX_E2E_PROVIDER"]
    return build_runtime(
        BuildOptions(
            provider=provider,
            model=os.environ.get("DCX_E2E_MODEL"),
            persist=False,
            memory_enabled=False,
            permission_mode="bypass",
            cwd=tmp_path,
        )
    )


async def test_live_text_turn(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    try:
        events = [
            event
            async for event in bundle.runtime.run_turn(
                "Reply with exactly: LIVE-OK"
            )
        ]
        text = "".join(
            event.text for event in events if isinstance(event, AgentTextDelta)
        )
        assert "LIVE-OK" in text
    finally:
        await bundle.runtime.close()


async def test_live_tool_cycle(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    try:
        events = [
            event
            async for event in bundle.runtime.run_turn(
                "Use the Bash tool to run: echo live-tool-ok. Then report the output."
            )
        ]
        finished = [event for event in events if isinstance(event, AgentToolFinished)]
        assert finished, "the model did not call a tool"
        assert any(
            "live-tool-ok" in block.text
            for event in finished
            for block in event.content
            if hasattr(block, "text")
        )
    finally:
        await bundle.runtime.close()
