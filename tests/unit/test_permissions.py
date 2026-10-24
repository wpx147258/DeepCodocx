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
SHELL = ToolSpec(
    "Bash", "shell", {"type": "object"}, category="shell", scope_arg="command"
)
EDIT = ToolSpec("Edit", "write", {"type": "object"}, category="write", path_arg="path")


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
    policy = PermissionPolicy(PermissionMode.DEFAULT, rules=rules, workspace=tmp_path)
    assert (await policy.authorize(SHELL, {"command": "git status"}, None)).allowed
    denied = await policy.authorize(SHELL, {"command": "git push origin"}, None)
    assert not denied.allowed


async def test_workspace_boundary_asks(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    asker = RecordingAsker("yes")
    outcome = await policy.authorize(READ, {"path": "/etc/hosts"}, asker)
    assert outcome.allowed
    assert asker.requests and "outside the workspace" in asker.requests[0].reason


async def test_always_is_scoped_to_exact_command(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    asker = RecordingAsker("always")
    first = await policy.authorize(SHELL, {"command": "echo 1"}, asker)
    same = await policy.authorize(SHELL, {"command": "echo 1"}, NullAsker())
    different_asker = RecordingAsker("no")
    different = await policy.authorize(SHELL, {"command": "echo 2"}, different_asker)

    assert first.allowed and first.remembered
    assert same.allowed
    assert not different.allowed
    assert len(asker.requests) == 1
    assert len(different_asker.requests) == 1
    assert "command: echo 1" in asker.requests[0].scope


async def test_always_is_scoped_to_exact_path(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    asker = RecordingAsker("always")
    first = await policy.authorize(WRITE, {"path": "a.txt"}, asker)
    same = await policy.authorize(WRITE, {"path": "a.txt"}, NullAsker())
    different_asker = RecordingAsker("no")
    different = await policy.authorize(WRITE, {"path": "b.txt"}, different_asker)

    assert first.allowed and same.allowed
    assert not different.allowed
    assert "path:" in asker.requests[0].scope


async def test_composed_shell_is_not_auto_allowed_by_glob_rule(tmp_path: Path) -> None:
    rules = PermissionRules(
        allow=(PermissionRule("Bash", {"command": "git*"}),),
    )
    policy = PermissionPolicy(PermissionMode.DEFAULT, rules=rules, workspace=tmp_path)
    assert (await policy.authorize(SHELL, {"command": "git status"}, None)).allowed

    asker = RecordingAsker("no")
    denied = await policy.authorize(
        SHELL, {"command": "git status; rm -rf tmp"}, asker
    )
    assert not denied.allowed
    assert asker.requests
    assert "shell composition" in asker.requests[0].reason


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_read_is_allowed_in_every_mode(tmp_path: Path, mode: PermissionMode) -> None:
    policy = PermissionPolicy(mode, workspace=tmp_path)
    assert (await policy.authorize(READ, {"path": "a.txt"}, None)).allowed


@pytest.mark.parametrize(
    "operator", [";", "&", "&&", "|", "||", ">", ">>", "<", "`", "$(", "(", ")", "\n", "&>"]
)
async def test_each_composition_operator_requires_approval(
    tmp_path: Path, operator: str
) -> None:
    rules = PermissionRules(allow=(PermissionRule("Bash", {"command": "git*"}),))
    policy = PermissionPolicy(PermissionMode.DEFAULT, rules=rules, workspace=tmp_path)
    asker = RecordingAsker("no")
    outcome = await policy.authorize(
        SHELL, {"command": f"git status {operator} rm -rf x"}, asker
    )
    assert not outcome.allowed
    assert asker.requests


@pytest.mark.parametrize(
    "command",
    [
        "git -c alias.p='!rm -rf /' p",
        "find . -exec rm {} +",
        "sh -c 'rm -rf /'",
        "xargs rm",
        "env rm -rf /",
    ],
)
async def test_shell_launchers_require_approval(tmp_path: Path, command: str) -> None:
    rules = PermissionRules(allow=(PermissionRule("Bash", {}),))
    policy = PermissionPolicy(PermissionMode.DEFAULT, rules=rules, workspace=tmp_path)
    asker = RecordingAsker("no")
    outcome = await policy.authorize(SHELL, {"command": command}, asker)
    assert not outcome.allowed
    assert asker.requests


async def test_composed_shell_always_is_exact(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    asker = RecordingAsker("always")
    command = "git status; rm -rf a"
    first = await policy.authorize(SHELL, {"command": command}, asker)
    same = await policy.authorize(SHELL, {"command": command}, NullAsker())
    different_asker = RecordingAsker("no")
    different = await policy.authorize(
        SHELL, {"command": "git status; rm -rf b"}, different_asker
    )
    assert first.allowed and same.allowed
    assert not different.allowed
    assert different_asker.requests


async def test_always_path_normalizes_relative_absolute_and_dotdot(
    tmp_path: Path,
) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    asker = RecordingAsker("always")
    assert (await policy.authorize(WRITE, {"path": "a.txt"}, asker)).allowed
    assert (await policy.authorize(WRITE, {"path": "./a.txt"}, NullAsker())).allowed
    assert (await policy.authorize(WRITE, {"path": "sub/../a.txt"}, NullAsker())).allowed
    absolute = str((tmp_path / "a.txt").resolve())
    assert (await policy.authorize(WRITE, {"path": absolute}, NullAsker())).allowed
    different = RecordingAsker("no")
    assert not (await policy.authorize(WRITE, {"path": "b.txt"}, different)).allowed


async def test_always_does_not_cross_tools(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    asker = RecordingAsker("always")
    assert (await policy.authorize(WRITE, {"path": "a.txt"}, asker)).allowed
    different = RecordingAsker("no")
    assert not (await policy.authorize(EDIT, {"path": "a.txt"}, different)).allowed
    assert different.requests


async def test_missing_or_non_string_command_does_not_widen_scope(
    tmp_path: Path,
) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    asker = RecordingAsker("always")
    first = await policy.authorize(SHELL, {}, asker)
    assert first.allowed
    assert not (await policy.authorize(SHELL, {"command": "rm -rf /"}, NullAsker())).allowed


async def test_empty_path_does_not_widen_scope(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    asker = RecordingAsker("always")
    assert (await policy.authorize(WRITE, {"path": ""}, asker)).allowed
    assert not (await policy.authorize(WRITE, {"path": "a.txt"}, NullAsker())).allowed


async def test_outside_workspace_always_is_scoped(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    asker = RecordingAsker("always")
    assert (await policy.authorize(READ, {"path": "/etc/hosts"}, asker)).allowed
    assert (await policy.authorize(READ, {"path": "/etc/hosts"}, NullAsker())).allowed
    different = RecordingAsker("no")
    assert not (await policy.authorize(READ, {"path": "/etc/passwd"}, different)).allowed
    assert different.requests


async def test_resolve_error_fails_closed(tmp_path: Path, monkeypatch) -> None:
    policy = PermissionPolicy(PermissionMode.DEFAULT, workspace=tmp_path)
    original = Path.resolve

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        if self.name == "loop":
            raise RuntimeError("symlink loop")
        return original(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    outcome = await policy.authorize(READ, {"path": "loop"}, NullAsker())
    assert not outcome.allowed


async def test_quoted_operator_is_conservatively_asked(tmp_path: Path) -> None:
    rules = PermissionRules(allow=(PermissionRule("Bash", {"command": "git*"}),))
    policy = PermissionPolicy(PermissionMode.DEFAULT, rules=rules, workspace=tmp_path)
    asker = RecordingAsker("no")
    outcome = await policy.authorize(
        SHELL, {"command": "git log --format='%h <%ae>'"}, asker
    )
    assert not outcome.allowed
    assert asker.requests


async def test_bypass_mode_still_allows_composed_command(tmp_path: Path) -> None:
    rules = PermissionRules(allow=(PermissionRule("Bash", {"command": "git*"}),))
    policy = PermissionPolicy(PermissionMode.BYPASS, rules=rules, workspace=tmp_path)
    assert (
        await policy.authorize(SHELL, {"command": "git status; rm -rf x"}, None)
    ).allowed
