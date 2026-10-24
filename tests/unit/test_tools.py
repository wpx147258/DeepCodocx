from __future__ import annotations

import base64
import shlex
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from cli_agent.messages import IMAGE_UNSUPPORTED_NOTE, ImageBlock
from cli_agent.tools.base import ToolContext
from cli_agent.tools.builtin.bash import BashTool
from cli_agent.tools.builtin.files import (
    MAX_IMAGE_BYTES,
    EditTool,
    ReadTool,
    WriteTool,
)
from cli_agent.tools.builtin.search import GlobTool, GrepTool


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(cwd=tmp_path, session_id="test", data_dir=tmp_path)


async def test_write_read_round_trip(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    write = await WriteTool().run({"path": "notes.txt", "content": "one\ntwo\n"}, ctx)
    assert not write.is_error
    read = await ReadTool().run({"path": "notes.txt"}, ctx)
    assert "one" in read.text_content()
    assert "1\t" in read.text_content()
    assert not list(tmp_path.glob("*.tmp"))


async def test_edit_requires_unique_match(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (tmp_path / "a.txt").write_text("same\nsame\n", encoding="utf-8")
    ambiguous = await EditTool().run(
        {"path": "a.txt", "old_string": "same", "new_string": "other"}, ctx
    )
    assert ambiguous.is_error
    assert "found 2 times" in ambiguous.text_content()

    replaced = await EditTool().run(
        {"path": "a.txt", "old_string": "same", "new_string": "other", "replace_all": True},
        ctx,
    )
    assert not replaced.is_error
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "other\nother\n"


async def test_edit_returns_diff(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (tmp_path / "a.txt").write_text("old line\n", encoding="utf-8")
    result = await EditTool().run(
        {"path": "a.txt", "old_string": "old line", "new_string": "new line"}, ctx
    )
    assert not result.is_error
    assert "-old line" in result.text_content()
    assert "+new line" in result.text_content()


async def test_glob_and_grep(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "one.py").write_text("needle = 1\n", encoding="utf-8")
    (tmp_path / "src" / "two.txt").write_text("nothing\n", encoding="utf-8")
    globbed = await GlobTool().run({"pattern": "**/*.py"}, ctx)
    assert "one.py" in globbed.text_content()
    assert "two.txt" not in globbed.text_content()

    grepped = await GrepTool().run(
        {"pattern": "needle", "path": "src", "output_mode": "content"}, ctx
    )
    assert "needle" in grepped.text_content()
    assert "one.py" in grepped.text_content()


async def test_bash_runs_in_workspace(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await BashTool().run({"command": "echo tool-ok"}, ctx)
    assert not result.is_error
    assert "tool-ok" in result.text_content()


async def test_read_missing_file_is_an_error(tmp_path: Path) -> None:
    result = await ReadTool().run({"path": "nope.txt"}, _ctx(tmp_path))
    assert result.is_error
    assert "does not exist" in result.text_content()


async def test_bash_reports_exit_code_marker(tmp_path: Path) -> None:
    result = await BashTool().run({"command": "exit 7"}, _ctx(tmp_path))
    assert "[exit code: 7]" in result.text_content()


async def test_bash_large_output_is_truncated(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    python = shlex.quote(sys.executable)
    result = await BashTool().run(
        {
            "command": f"{python} -c \"import sys; sys.stdout.write('x' * 200000)\"",
            "timeout": 10,
        },
        ctx,
    )
    assert not result.is_error
    text = result.text_content()
    assert "bytes omitted" in text
    assert "[output truncated; command terminated]" in text
    assert "[exit code: -9]" not in text
    assert len(text) < 60_000


async def test_bash_timeout_returns_error(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await BashTool().run(
        {
            "command": f"{shlex.quote(sys.executable)} -c \"import time; time.sleep(5)\"",
            "timeout": 1,
        },
        ctx,
    )
    assert result.is_error
    assert "timed out" in result.text_content()


async def test_bash_stderr_only_overflow(tmp_path: Path) -> None:
    python = shlex.quote(sys.executable)
    result = await BashTool().run(
        {
            "command": f"{python} -c \"import sys; sys.stderr.write('e' * 200000)\"",
            "timeout": 10,
        },
        _ctx(tmp_path),
    )
    assert not result.is_error
    assert "bytes omitted" in result.text_content()


async def test_bash_binary_output_is_bounded(tmp_path: Path) -> None:
    python = shlex.quote(sys.executable)
    result = await BashTool().run(
        {
            "command": (
                f"{python} -c \"import sys; "
                "sys.stdout.buffer.write(bytes(range(256)) * 400)\""
            ),
            "timeout": 10,
        },
        _ctx(tmp_path),
    )
    assert not result.is_error
    assert len(result.text_content()) < 60_000


async def test_read_image_omits_base64_when_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"\x89PNG\r\n\x1a\nnot-really-a-png"
    (tmp_path / "pic.png").write_bytes(payload)
    reads: list[Path] = []
    original = Path.read_bytes

    def spy(path: Path) -> bytes:
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", spy)
    result = await ReadTool().run({"path": "pic.png"}, _ctx(tmp_path))
    assert not result.is_error
    assert result.text_content() == IMAGE_UNSUPPORTED_NOTE
    assert not any(isinstance(block, ImageBlock) for block in result.content)
    assert reads == []


@pytest.mark.parametrize("supported", [False, True])
async def test_read_image_over_limit_errors(tmp_path: Path, supported: bool) -> None:
    path = tmp_path / "big.png"
    path.write_bytes(b"")
    with path.open("r+b") as handle:
        handle.truncate(MAX_IMAGE_BYTES + 1)
    ctx = replace(_ctx(tmp_path), supports_images=supported)
    result = await ReadTool().run({"path": "big.png"}, ctx)
    assert result.is_error
    assert "too large" in result.text_content()
    assert not any(isinstance(block, ImageBlock) for block in result.content)


async def test_read_image_returns_block_when_supported(tmp_path: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\nnot-really-a-png"
    (tmp_path / "pic.png").write_bytes(payload)
    ctx = replace(_ctx(tmp_path), supports_images=True)
    result = await ReadTool().run({"path": "pic.png"}, ctx)
    assert not result.is_error
    images = [block for block in result.content if isinstance(block, ImageBlock)]
    assert len(images) == 1
    assert images[0].media_type == "image/png"
    assert base64.b64decode(images[0].data) == payload
    assert IMAGE_UNSUPPORTED_NOTE not in result.text_content()


async def test_read_text_file_when_images_supported(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    ctx = replace(_ctx(tmp_path), supports_images=True)
    result = await ReadTool().run({"path": "notes.txt"}, ctx)
    assert not result.is_error
    assert "hello" in result.text_content()
