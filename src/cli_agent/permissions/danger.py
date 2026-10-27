"""A conservative static check for obviously destructive shell commands.

This is approval UX, not a sandbox. It recognizes a handful of recursive deletes
aimed at the filesystem root, the home directory, a parent directory, the
current directory, or a system directory, and forces one confirmation.

It deliberately does not interpret shell semantics: ``sh -c "..."``, ``xargs``,
a script, or an interpreter one-liner can hide anything. Keep the list short and
high precision; a false positive costs one prompt, but a noisy guard gets turned
off.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass

_CONTROL = {"&&", "||", ";", "|", "&", "(", ")"}
_WRAPPERS = {"sudo", "command", "env", "nohup", "nice", "time", "exec", "stdbuf"}
# Wrapper flags that consume the following token (sudo -u root, ...).
_WRAPPER_VALUE_FLAGS = {"-u", "-g", "-p", "-C", "-h", "-r", "-t", "-U", "-c"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_ROOT = frozenset({"/", "/*", "/**"})
_HOME = frozenset(
    {"~", "~/*", "$HOME", "$HOME/*", "${HOME}", "${HOME}/*", "~/.", "~/.?*"}
)
_PARENT = frozenset({"..", "../", "../*", "../.", "../..", "../../"})
_CURRENT = frozenset({".", "./", "./*", "*", ".*", ".??*"})
_SYSTEM = frozenset(
    name
    for directory in (
        "/home",
        "/root",
        "/usr",
        "/etc",
        "/var",
        "/boot",
        "/bin",
        "/lib",
        "/opt",
        "/sbin",
    )
    for name in (directory, f"{directory}/*")
)

_REASONS = {
    "root": "recursive delete of the filesystem root",
    "home": "recursive delete of the home directory",
    "system": "recursive delete of a system directory",
    "parent": "recursive delete of a parent directory",
    "current": "recursive delete of the current directory",
}


@dataclass(frozen=True, slots=True)
class Danger:
    target: str
    reason: str


def classify(command: str) -> Danger | None:
    """Return the first high-risk delete found in ``command``, if any."""
    for segment in _segments(command):
        danger = _check_segment(segment)
        if danger is not None:
            return danger
    return None


def _segments(command: str) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in _tokenize(command):
        if token in _CONTROL:
            if current:
                segments.append(current)
            current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _tokenize(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        # Unbalanced quotes: fall back to a plain split rather than skipping the
        # check entirely.
        return command.split()


def _check_segment(tokens: list[str]) -> Danger | None:
    tokens = _strip_wrappers(tokens)
    if not tokens or os.path.basename(tokens[0]) != "rm":
        return None
    recursive, targets = _rm_arguments(tokens[1:])
    if not recursive:
        return None
    for target in targets:
        reason = _target_reason(target)
        if reason is not None:
            return Danger(target=target, reason=reason)
    return None


def _strip_wrappers(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ASSIGNMENT.match(token):
            index += 1
            continue
        if token not in _WRAPPERS:
            break
        index += 1
        # Walk past the wrapper's own flags, e.g. `sudo -u root ...`.
        while index < len(tokens) and tokens[index].startswith("-"):
            flag = tokens[index]
            index += 1
            if flag in _WRAPPER_VALUE_FLAGS and index < len(tokens):
                index += 1
    return tokens[index:]


def _rm_arguments(tokens: list[str]) -> tuple[bool, list[str]]:
    recursive = False
    targets: list[str] = []
    options_done = False
    for token in tokens:
        if not options_done and token == "--":
            options_done = True
            continue
        if not options_done and token.startswith("--") and len(token) > 2:
            if token == "--recursive":
                recursive = True
            continue
        if not options_done and token.startswith("-") and token != "-":
            if "r" in token[1:] or "R" in token[1:]:
                recursive = True
            continue
        targets.append(token)
    return recursive, targets


def _target_reason(raw: str) -> str | None:
    target = raw.rstrip("/") or "/"
    if target in _ROOT:
        return _REASONS["root"]
    if target in _HOME:
        return _REASONS["home"]
    if target in _PARENT:
        return _REASONS["parent"]
    if target in _CURRENT:
        return _REASONS["current"]
    if target in _SYSTEM:
        return _REASONS["system"]
    return None
