from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cli_agent.errors import ConfigError
from cli_agent.permissions.controller import PermissionController
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


def test_controller_persists_mode_changes(tmp_path: Path) -> None:
    changes: list[tuple[str, str | None]] = []
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    controller = PermissionController(
        policy, on_change=lambda mode, previous: changes.append((mode, previous))
    )

    assert controller.toggle_auto() == "bypass"
    assert changes == [("bypass", "default")]
    assert controller.toggle_auto() == "default"
    assert changes[-1] == ("default", None)


def test_controller_restore_does_not_persist(tmp_path: Path) -> None:
    changes: list[tuple[str, str | None]] = []
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    controller = PermissionController(
        policy, on_change=lambda mode, previous: changes.append((mode, previous))
    )
    controller.restore("bypass", "accept-edits")
    assert controller.mode == "bypass"
    assert changes == []
    # /auto off returns to the mode that was active before auto was enabled.
    assert controller.set_auto(False) == "accept-edits"


def test_controller_rejects_unknown_mode(tmp_path: Path) -> None:
    controller = PermissionController(
        PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    )
    with pytest.raises(ConfigError):
        controller.set_mode("yolo")


def test_controller_ignores_unknown_restored_mode(tmp_path: Path) -> None:
    controller = PermissionController(
        PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    )
    controller.restore("yolo")
    assert controller.mode == "default"


async def test_high_risk_delete_asks_even_in_bypass(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.BYPASS, workspace=tmp_path)
    asker = RecordingAsker("yes")
    outcome = await policy.authorize(SHELL, {"command": "rm -rf /"}, asker)
    assert outcome.allowed
    assert asker.requests
    assert "high-risk" in asker.requests[0].reason
    assert "filesystem root" in asker.requests[0].reason


async def test_high_risk_delete_fails_closed_without_an_asker(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.BYPASS, workspace=tmp_path)
    for asker in (None, NullAsker()):
        outcome = await policy.authorize(SHELL, {"command": "rm -rf /"}, asker)
        assert not outcome.allowed
        assert "high-risk" in outcome.reason


async def test_high_risk_delete_is_never_remembered(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.ACCEPT_EDITS, workspace=tmp_path)
    asker = RecordingAsker("always")
    for command in ("rm -rf /", "rm -rf ~", "rm -rf *"):
        assert (await policy.authorize(SHELL, {"command": command}, asker)).allowed
    # Answering "always" must not turn the guard off for later commands.
    assert len(asker.requests) == 3


async def test_safe_shell_commands_keep_the_mode_shortcuts(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.BYPASS, workspace=tmp_path)
    asker = RecordingAsker("yes")
    outcome = await policy.authorize(SHELL, {"command": "rm -rf build"}, asker)
    assert outcome.allowed
    assert asker.requests == []


async def test_explicit_allow_rule_beats_the_guard(tmp_path: Path) -> None:
    rules = PermissionRules(allow=(PermissionRule("Bash", {"command": "rm -rf /tmp/*"}),))
    policy = PermissionPolicy(PermissionMode.DEFAULT, rules=rules, workspace=tmp_path)
    outcome = await policy.authorize(
        SHELL, {"command": "rm -rf /tmp/*"}, NullAsker()
    )
    assert outcome.allowed
    assert "configuration rule" in outcome.reason
