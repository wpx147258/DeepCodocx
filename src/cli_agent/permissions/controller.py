"""Permission mode switching and /auto bookkeeping.

The controller owns *which* mode is active; the policy still decides whether an
individual call is allowed. Persistence is a callback so this module does not
depend on the session store.
"""

from __future__ import annotations

from collections.abc import Callable

from ..errors import ConfigError
from .policy import PermissionMode, PermissionPolicy

PermissionChange = Callable[[str, str | None], None]


class PermissionController:
    def __init__(
        self,
        policy: PermissionPolicy,
        *,
        on_change: PermissionChange | None = None,
    ) -> None:
        self._policy = policy
        self._on_change = on_change
        # Mode to restore when /auto is switched off again.
        self._auto_previous: str | None = None

    @property
    def mode(self) -> str:
        return self._policy.mode.value

    def _apply(self, mode: str, *, persist: bool) -> str:
        try:
            effective = self._policy.set_mode(mode).value
        except ValueError as exc:
            raise ConfigError(f"unknown permission mode {mode!r}") from exc
        if persist and self._on_change is not None:
            self._on_change(effective, self._auto_previous)
        return effective

    def set_mode(self, mode: str, *, persist: bool = True) -> str:
        """Switch modes explicitly; any /auto bookkeeping is dropped."""
        self._auto_previous = None
        return self._apply(mode, persist=persist)

    def set_auto(self, enabled: bool, *, persist: bool = True) -> str:
        """Run tools without asking. Remembers the mode it replaced."""
        if enabled:
            if self._policy.mode is not PermissionMode.BYPASS:
                self._auto_previous = self._policy.mode.value
                return self._apply(PermissionMode.BYPASS.value, persist=persist)
            return self.mode
        if self._auto_previous is not None:
            previous, self._auto_previous = self._auto_previous, None
            return self._apply(previous, persist=persist)
        # Bypass was configured from outside /auto; leave it alone.
        return self.mode

    def toggle_auto(self, *, persist: bool = True) -> str:
        return self.set_auto(
            self._policy.mode is not PermissionMode.BYPASS, persist=persist
        )

    def restore(self, mode: str | None, auto_previous: str | None = None) -> None:
        """Apply a mode recorded in the session without appending a new record."""
        if mode is None:
            return
        try:
            effective = self._apply(mode, persist=False)
        except ConfigError:
            return
        self._auto_previous = auto_previous if effective == "bypass" else None
