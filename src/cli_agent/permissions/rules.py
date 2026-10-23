"""Allow/deny rules loaded from TOML.

Rules are approval convenience, not a sandbox. A shell command can still escape
a glob rule; use the permission mode and an external sandbox for real isolation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Literal

from ..tools.base import ToolSpec

RuleDecision = Literal["allow", "deny"]


@dataclass(frozen=True, slots=True)
class PermissionRule:
    tool: str
    args: Mapping[str, str] = field(default_factory=dict)

    def matches(self, spec: ToolSpec, tool_input: Mapping[str, Any]) -> bool:
        if self.tool not in ("", "*") and not fnmatch(spec.name, self.tool):
            return False
        for key, pattern in self.args.items():
            value = tool_input.get(key)
            if value is None or not fnmatch(str(value), pattern):
                return False
        return True


@dataclass(frozen=True, slots=True)
class PermissionRules:
    allow: tuple[PermissionRule, ...] = ()
    deny: tuple[PermissionRule, ...] = ()

    @classmethod
    def from_config(
        cls,
        allow: tuple[PermissionRule, ...] | list[PermissionRule] = (),
        deny: tuple[PermissionRule, ...] | list[PermissionRule] = (),
    ) -> PermissionRules:
        return cls(allow=tuple(allow), deny=tuple(deny))

    def decide(self, spec: ToolSpec, tool_input: Mapping[str, Any]) -> RuleDecision | None:
        for rule in self.deny:
            if rule.matches(spec, tool_input):
                return "deny"
        for rule in self.allow:
            if rule.matches(spec, tool_input):
                return "allow"
        return None
