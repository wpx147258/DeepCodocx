"""Terminal input helpers: readline setup and multiline continuation."""

from __future__ import annotations

import sys

try:
    import readline

    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:  # pragma: no cover - Windows / minimal builds
    readline = None  # type: ignore[assignment]


def styled_prompt(text: str) -> str:
    if not sys.stdin.isatty():
        return text
    # \001/\002 tell Readline the ANSI escapes have zero display width.
    return f"\001\033[36m\002{text}\001\033[0m\002"


def read_user_input(prompt: str = "dcx > ") -> str | None:
    """Read one user message. Returns None on EOF; KeyboardInterrupt propagates."""
    try:
        first = input(styled_prompt(prompt))
    except EOFError:
        return None
    if not _needs_continuation(first):
        return first

    lines = [first.rstrip("\\")]
    while _needs_continuation("\n".join(lines)):
        try:
            lines.append(input(styled_prompt("... ")))
        except EOFError:
            break
    return "\n".join(lines)


def _needs_continuation(text: str) -> bool:
    stripped = text.rstrip()
    if stripped.endswith("\\"):
        return True
    if stripped.count('"""') % 2 == 1 or stripped.count("'''") % 2 == 1:
        return True
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    for char in text:
        if char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values() and stack and stack[-1] == char:
            stack.pop()
    return bool(stack)
