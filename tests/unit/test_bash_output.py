from __future__ import annotations

import os
import re

import pytest

from cli_agent.tools.builtin.bash import _BoundedOutput, _kill_tree


def test_bounded_output_under_limit() -> None:
    output = _BoundedOutput(100)
    output.feed(b"abc")
    output.feed(b"def")
    assert not output.overflow
    assert output.total == 6
    assert output.text() == "abcdef"


def test_bounded_output_exact_limit_is_not_overflow() -> None:
    output = _BoundedOutput(6)
    output.feed(b"abcdef")
    assert not output.overflow
    assert output.text() == "abcdef"


def test_bounded_output_omitted_count_matches_hidden_bytes() -> None:
    output = _BoundedOutput(50_000)
    for _ in range(20):
        output.feed(b"x" * 8_192)
    text = output.text()
    match = re.search(r"\[(\d+) bytes omitted\]", text)
    assert match is not None
    reported = int(match.group(1))
    half = max(1, (output.limit - 64) // 2)
    hidden = output.total - len(output.head[:half]) - len(output.tail[-half:])
    assert reported == hidden


@pytest.mark.parametrize("limit", [1, 3, 10])
def test_bounded_output_small_limits(limit: int) -> None:
    output = _BoundedOutput(limit)
    output.feed(b"abcdef" * 4)
    text = output.text()
    assert "bytes omitted" in text


def test_kill_tree_attempts_group_even_if_leader_exited(monkeypatch) -> None:
    if os.name == "nt":
        pytest.skip("POSIX process-group test")
    calls: list[tuple[int, int]] = []

    class Proc:
        returncode = 0
        pid = 12345
        def kill(self) -> None:  # pragma: no cover - fallback only
            calls.append((0, 0))

    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    _kill_tree(Proc())  # type: ignore[arg-type]
    assert calls and calls[0][0] == 12345
