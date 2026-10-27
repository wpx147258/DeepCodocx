from __future__ import annotations

import pytest

from cli_agent.permissions.danger import classify

DANGEROUS = [
    "rm -rf /",
    "rm -fr /",
    "rm --recursive --force /",
    "rm -rf -- /",
    "sudo rm -rf /",
    "sudo -u root rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    "rm -rf $HOME",
    'rm -rf "$HOME"',
    'rm -rf "${HOME}"',
    "rm -rf ~/*",
    "rm -rf ..",
    "rm -rf ../*",
    "rm -rf *",
    "rm -rf .",
    "rm -rf ./*",
    "rm -rf .*",
    "cd /tmp && rm -rf /etc",
    "echo hi; rm -rf /usr",
    "ls | rm -rf /var",
    "rm -rf /home",
    "rm -rf /root",
]

SAFE = [
    "rm file.txt",
    "rm -f build.log",
    "rm -rf build/",
    "rm -rf ./build",
    "rm -rf /tmp/scratch",
    "rm -rf node_modules",
    "rm -rf ~/Downloads/tmp",
    "git rm -rf .",
    "echo rm -rf /",
    "rg 'rm -rf /' .",
    "find . -name '*.pyc' -delete",
    "sh -c 'rm -rf /'",
]


@pytest.mark.parametrize("command", DANGEROUS)
def test_dangerous_deletes_are_flagged(command: str) -> None:
    assert classify(command) is not None


@pytest.mark.parametrize("command", SAFE)
def test_safe_or_unrelated_commands_are_not_flagged(command: str) -> None:
    assert classify(command) is None


def test_reasons_name_the_target() -> None:
    root = classify("rm -rf /")
    home = classify("rm -rf ~")
    current = classify("rm -rf *")
    parent = classify("rm -rf ..")
    system = classify("rm -rf /etc")
    assert root is not None and "filesystem root" in root.reason
    assert home is not None and "home directory" in home.reason
    assert current is not None and "current directory" in current.reason
    assert parent is not None and "parent directory" in parent.reason
    assert system is not None and "system directory" in system.reason


def test_unbalanced_quotes_still_screen_the_obvious_case() -> None:
    assert classify("rm -rf / 'unclosed") is not None


def test_non_recursive_delete_is_out_of_scope() -> None:
    assert classify("rm -f /etc/hosts") is None
