"""Exceptions shared by the agent runtime and its hosts."""

from __future__ import annotations


class DcxError(Exception):
    """Base class for errors that should be shown to the user."""


class ConfigError(DcxError):
    """Invalid or missing configuration."""


class SessionError(DcxError):
    """A session could not be created, locked, or resumed."""


class ToolInputError(DcxError):
    """A tool call did not match its declared JSON Schema."""


class ProviderFailure(Exception):
    """A provider error that escaped the event stream."""

    def __init__(self, message: str, kind: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status_code = status_code
