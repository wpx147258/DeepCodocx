"""Permission policy: modes, rules, workspace boundary, and interactive asks.

The approval gate is deliberately fail-closed. A non-interactive host (print
mode, tests, future background work) cannot answer an ``ask``; it becomes a
denial rather than an implicit yes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from ..tools.base import ToolSpec
from .danger import classify
from .rules import PermissionRules

type Answer = Literal["yes", "no", "always"]


class PermissionMode(StrEnum):
    BYPASS = "bypass"
    ACCEPT_EDITS = "accept-edits"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    tool_name: str
    tool_input: Mapping[str, Any]
    reason: str


class Asker(Protocol):
    async def ask(self, request: PermissionRequest) -> Answer: ...


class NullAsker:
    """An asker for non-interactive hosts. Every request is denied."""

    async def ask(self, request: PermissionRequest) -> Answer:  # noqa: ARG002
        return "no"


@dataclass(frozen=True, slots=True)
class PermissionOutcome:
    allowed: bool
    reason: str = ""
    remembered: bool = False


class PermissionPolicy:
    def __init__(
        self,
        mode: PermissionMode | str,
        *,
        rules: PermissionRules | None = None,
        workspace: Path | None = None,
    ) -> None:
        self.mode = PermissionMode(mode)
        self.rules = rules or PermissionRules()
        self.workspace = (workspace or Path.cwd()).resolve()
        self._always_allow: set[str] = set()

    def set_mode(self, mode: PermissionMode | str) -> PermissionMode:
        """Change the approval mode; raises ``ValueError`` for unknown modes."""
        self.mode = PermissionMode(mode)
        return self.mode

    async def authorize(
        self,
        spec: ToolSpec,
        tool_input: Mapping[str, Any],
        asker: Asker | None = None,
    ) -> PermissionOutcome:
        rule_decision = self.rules.decide(spec, tool_input)
        if rule_decision == "deny":
            return PermissionOutcome(False, "denied by configuration rule")
        if rule_decision == "allow":
            return PermissionOutcome(True, "allowed by configuration rule")

        # A destructive delete is never covered by /auto, a session-wide
        # "always", or the mode shortcuts: it needs one explicit confirmation.
        danger = _danger_reason(spec, tool_input)
        if danger is not None:
            outcome = await self._ask(
                asker,
                PermissionRequest(
                    tool_name=spec.name,
                    tool_input=tool_input,
                    reason=f"high-risk command: {danger}",
                ),
                spec.name,
                remember=False,
                non_interactive_reason=(
                    f"high-risk command requires interactive approval: {danger}"
                ),
            )
            if not outcome.allowed:
                return outcome
            return PermissionOutcome(True, f"approved once: {danger}")

        if spec.name in self._always_allow:
            return PermissionOutcome(True, "previously allowed for this session")

        outside = self._outside_workspace(spec, tool_input)
        if outside and self.mode is not PermissionMode.BYPASS:
            return await self._ask(
                asker,
                PermissionRequest(
                    tool_name=spec.name,
                    tool_input=tool_input,
                    reason=f"path is outside the workspace: {outside}",
                ),
                spec.name,
            )

        if self.mode is PermissionMode.BYPASS:
            return PermissionOutcome(True, "bypass mode")
        if spec.category == "read":
            return PermissionOutcome(True, "read-only tool")
        if self.mode is PermissionMode.ACCEPT_EDITS and spec.category == "write":
            return PermissionOutcome(True, "accept-edits mode")

        return await self._ask(
            asker,
            PermissionRequest(
                tool_name=spec.name,
                tool_input=tool_input,
                reason=f"{spec.category} tool in {self.mode.value} mode",
            ),
            spec.name,
        )

    async def _ask(
        self,
        asker: Asker | None,
        request: PermissionRequest,
        tool_name: str,
        *,
        remember: bool = True,
        non_interactive_reason: str | None = None,
    ) -> PermissionOutcome:
        if asker is None or isinstance(asker, NullAsker):
            return PermissionOutcome(
                False,
                non_interactive_reason
                or "approval required but the session is non-interactive",
            )
        try:
            answer = await asker.ask(request)
        except (EOFError, KeyboardInterrupt, OSError):
            return PermissionOutcome(False, "approval cancelled")
        if answer == "always":
            if not remember:
                return PermissionOutcome(True, "approved once")
            self._always_allow.add(tool_name)
            return PermissionOutcome(True, "approved for this session", remembered=True)
        if answer == "yes":
            return PermissionOutcome(True, "approved")
        return PermissionOutcome(False, "denied by user")

    def _outside_workspace(self, spec: ToolSpec, tool_input: Mapping[str, Any]) -> str | None:
        if spec.path_arg is None:
            return None
        raw = tool_input.get(spec.path_arg)
        if not isinstance(raw, str) or not raw:
            return None
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError):
            return raw
        try:
            resolved.relative_to(self.workspace)
        except ValueError:
            return str(resolved)
        return None


def _danger_reason(spec: ToolSpec, tool_input: Mapping[str, Any]) -> str | None:
    """Reason string when a shell command trips the destructive-command guard."""
    if spec.category != "shell":
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    danger = classify(command)
    return danger.reason if danger is not None else None
