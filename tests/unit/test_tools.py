from __future__ import annotations

from pathlib import Path

from cli_agent.tools.base import ToolContext
from cli_agent.tools.builtin.bash import BashTool
from cli_agent.tools.builtin.files import EditTool, ReadTool, WriteTool
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
