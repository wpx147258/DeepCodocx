from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cli_agent.permissions.policy import (
    NullAsker,
    PermissionMode,
    PermissionPolicy,
    PermissionRequest,
)
from cli_agent.permissions.rules import PermissionRule, PermissionRules
from cli_agent.tools.base import ToolSpec

READ = ToolSpec("Read", "read", {"type": "object"}, category="read", path_arg="path")
WRITE = ToolSpec("Write", "write", {"type": "object"}, category="write", path_arg="path")
SHELL = ToolSpec("Bash", "shell", {"type": "object"}, category="shell")


class RecordingAsker:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.requests: list[PermissionRequest] = []

    async def ask(self, request: PermissionRequest) -> Any:
        self.requests.append(request)
        return self.answer


async def test_bypass_allows_shell(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.BYPASS, workspace=tmp_path)
    outcome = await policy.authorize(SHELL, {"command": "echo hi"}, None)
    assert outcome.allowed


async def test_accept_edits_allows_write_and_asks_shell(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.ACCEPT_EDITS, workspace=tmp_path)
    assert (await policy.authorize(WRITE, {"path": "a.txt"}, None)).allowed
    denied = await policy.authorize(SHELL, {"command": "echo hi"}, None)
    assert not denied.allowed


async def test_default_allows_read_and_asks_write(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    assert (await policy.authorize(READ, {"path": "a.txt"}, None)).allowed
    denied = await policy.authorize(WRITE, {"path": "a.txt"}, None)
    assert not denied.allowed


async def test_null_asker_never_allows(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    outcome = await policy.authorize(SHELL, {"command": "echo hi"}, NullAsker())
    assert not outcome.allowed
    assert "non-interactive" in outcome.reason


async def test_rule_deny_wins_over_allow(tmp_path: Path) -> None:
    rules = PermissionRules(
        allow=(PermissionRule("Bash", {"command": "git*"}),),
        deny=(PermissionRule("Bash", {"command": "git push*"}),),
    )
    policy = PermissionPolicy(PermissionMode.BYPASS, rules=rules, workspace=tmp_path)
    assert (await policy.authorize(SHELL, {"command": "git status"}, None)).allowed
    denied = await policy.authorize(SHELL, {"command": "git push origin"}, None)
    assert not denied.allowed


async def test_workspace_boundary_asks(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    asker = RecordingAsker("yes")
    outcome = await policy.authorize(READ, {"path": "/etc/hosts"}, asker)
    assert outcome.allowed
    assert asker.requests and "outside the workspace" in asker.requests[0].reason


async def test_always_remembers_tool(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    asker = RecordingAsker("always")
    first = await policy.authorize(SHELL, {"command": "echo 1"}, asker)
    second = await policy.authorize(SHELL, {"command": "echo 2"}, NullAsker())
    assert first.allowed and first.remembered
    assert second.allowed
    assert len(asker.requests) == 1


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_read_is_allowed_in_every_mode(tmp_path: Path, mode: PermissionMode) -> None:
    policy = PermissionPolicy(mode, workspace=tmp_path)
    assert (await policy.authorize(READ, {"path": "a.txt"}, None)).allowed
