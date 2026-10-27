"""Permission policy: modes, rules, workspace boundary, and interactive asks.

The approval gate is deliberately fail-closed. A non-interactive host cannot
answer an ``ask``; it becomes a denial rather than an implicit yes.

Allow/deny rules are approval convenience, not a sandbox. The shell check below
is a best-effort guard against common composition/launcher patterns; wrappers
and interpreters can still execute arbitrary commands.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol

from ..tools.base import ToolSpec
from .danger import classify
from .rules import PermissionRules

type Answer = Literal["yes", "no", "always"]

# Common shell composition operators. This is deliberately conservative: a
# false positive only causes an extra approval prompt.
_SHELL_OPERATOR_TOKENS = (";", "&", "|", ">", "<", "`", "$(", "(", ")", "\n")
# Common ways a seemingly simple command launches another program. Not
# exhaustive: rules remain approval UX, not a sandbox.
_SHELL_LAUNCHER_TOKENS = (
    " -c",
    "-exec",
    "--to-command",
    "alias.",
    "xargs",
    "env ",
    "eval ",
    "exec ",
)


class AlwaysScope(NamedTuple):
    """The exact scope remembered by an ``always`` decision."""

    tool: str
    field: str
    value: str


class PermissionMode(StrEnum):
    BYPASS = "bypass"
    ACCEPT_EDITS = "accept-edits"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    tool_name: str
    tool_input: Mapping[str, Any]
    reason: str
    scope: str = ""


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
        self._always_allow: set[AlwaysScope] = set()

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

        rule_skipped_for_composition = False
        if rule_decision == "allow":
            if _shell_rule_requires_approval(spec, tool_input):
                rule_skipped_for_composition = True
            else:
                return PermissionOutcome(True, "allowed by configuration rule")

        # A destructive delete is never covered by /auto, a session-wide
        # "always", or the mode shortcuts: it needs one explicit confirmation.
        # An explicit allow rule above is the only override.
        danger = _danger_reason(spec, tool_input)
        if danger is not None:
            outcome = await self._ask(
                asker,
                spec,
                tool_input,
                scope=self._scope_key(spec, tool_input),
                reason=f"high-risk command: {danger}",
                remember=False,
                non_interactive_reason=(
                    f"high-risk command requires interactive approval: {danger}"
                ),
            )
            if not outcome.allowed:
                return outcome
            return PermissionOutcome(True, f"approved once: {danger}")

        scope = self._scope_key(spec, tool_input)
        if scope in self._always_allow:
            return PermissionOutcome(True, "previously allowed for this session")

        outside = self._outside_workspace(spec, tool_input)
        if outside and self.mode is not PermissionMode.BYPASS:
            return await self._ask(
                asker,
                spec,
                tool_input,
                scope=scope,
                reason=f"path is outside the workspace: {outside}",
            )

        if self.mode is PermissionMode.BYPASS:
            return PermissionOutcome(True, "bypass mode")
        if spec.category == "read":
            return PermissionOutcome(True, "read-only tool")
        if self.mode is PermissionMode.ACCEPT_EDITS and spec.category == "write":
            return PermissionOutcome(True, "accept-edits mode")

        reason = f"{spec.category} tool in {self.mode.value} mode"
        if rule_skipped_for_composition:
            reason += "; allow rule skipped for shell composition or launcher"
        return await self._ask(
            asker,
            spec,
            tool_input,
            scope=scope,
            reason=reason,
        )

    async def _ask(
        self,
        asker: Asker | None,
        spec: ToolSpec,
        tool_input: Mapping[str, Any],
        *,
        scope: AlwaysScope,
        reason: str,
        remember: bool = True,
        non_interactive_reason: str | None = None,
    ) -> PermissionOutcome:
        if asker is None or isinstance(asker, NullAsker):
            return PermissionOutcome(
                False,
                non_interactive_reason
                or "approval required but the session is non-interactive",
            )
        request = PermissionRequest(
            tool_name=spec.name,
            tool_input=tool_input,
            reason=reason,
            scope=self._scope_label(spec, scope),
        )
        try:
            answer = await asker.ask(request)
        except (EOFError, KeyboardInterrupt, OSError):
            return PermissionOutcome(False, "approval cancelled")
        if answer == "always":
            if not remember:
                return PermissionOutcome(True, "approved once")
            self._always_allow.add(scope)
            return PermissionOutcome(
                True,
                f"approved for this session ({request.scope})",
                remembered=True,
            )
        if answer == "yes":
            return PermissionOutcome(True, "approved")
        return PermissionOutcome(False, "denied by user")

    def _scope_key(
        self, spec: ToolSpec, tool_input: Mapping[str, Any]
    ) -> AlwaysScope:
        field = spec.scope_arg or spec.path_arg
        if field is not None:
            raw = tool_input.get(field)
            if isinstance(raw, str) and raw:
                value = raw
                if field == spec.path_arg:
                    resolved = self._resolve_path(raw)
                    value = str(resolved) if resolved is not None else raw
                return AlwaysScope(spec.name, field, value)
        # A declared scope that is missing or malformed must not widen an
        # "always" decision to the whole tool. Key it by the exact input.
        return AlwaysScope(spec.name, "input", _input_digest(tool_input))

    def _scope_label(self, spec: ToolSpec, scope: AlwaysScope) -> str:
        if scope.field == "input":
            return f"tool {scope.tool} with this exact input ({scope.value[:8]})"
        if scope.field == "command":
            return f"command: {scope.value}"
        return f"{scope.field}: {scope.value}"

    def _resolve_path(self, raw: str) -> Path | None:
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self.workspace / path
            return path.resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    def _outside_workspace(self, spec: ToolSpec, tool_input: Mapping[str, Any]) -> str | None:
        if spec.path_arg is None:
            return None
        raw = tool_input.get(spec.path_arg)
        if not isinstance(raw, str) or not raw:
            return None
        resolved = self._resolve_path(raw)
        if resolved is None:
            return raw
        try:
            resolved.relative_to(self.workspace)
        except ValueError:
            return str(resolved)
        return None


def _shell_rule_requires_approval(
    spec: ToolSpec, tool_input: Mapping[str, Any]
) -> bool:
    if spec.category != "shell":
        return False
    field = spec.scope_arg or "command"
    command = tool_input.get(field)
    if not isinstance(command, str):
        return False
    tokens = (*_SHELL_OPERATOR_TOKENS, *_SHELL_LAUNCHER_TOKENS)
    return any(token in command for token in tokens)


def _input_digest(tool_input: Mapping[str, Any]) -> str:
    try:
        text = json.dumps(
            dict(tool_input), sort_keys=True, ensure_ascii=False, default=str
        )
    except (TypeError, ValueError):
        text = repr(tool_input)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _danger_reason(spec: ToolSpec, tool_input: Mapping[str, Any]) -> str | None:
    """Reason string when a shell command trips the destructive-command guard."""
    if spec.category != "shell":
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    danger = classify(command)
    return danger.reason if danger is not None else None
