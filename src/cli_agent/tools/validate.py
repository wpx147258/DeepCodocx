"""JSON Schema validation at tool dispatch."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .base import ToolSpec


def validate_tool_args(spec: ToolSpec, args: Mapping[str, Any]) -> str | None:
    """Return a human-readable error, or ``None`` when the input is valid."""
    try:
        validator = Draft202012Validator(dict(spec.input_schema))
        errors = sorted(validator.iter_errors(dict(args)), key=lambda error: list(error.path))
    except SchemaError as exc:
        return f"tool {spec.name!r} has an invalid input schema: {exc.message}"

    if not errors:
        return None

    messages = []
    for error in errors[:3]:
        location = ".".join(str(part) for part in error.absolute_path) or "(root)"
        messages.append(f"{location}: {error.message}")
    return f"invalid arguments for {spec.name}: " + "; ".join(messages)
