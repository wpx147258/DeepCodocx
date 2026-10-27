from __future__ import annotations

import base64
from pathlib import Path

from cli_agent.messages import ImageBlock
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


async def test_read_offset_past_end_is_reported(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    result = await ReadTool().run({"path": "a.txt", "offset": 99}, ctx)
    assert result.is_error
    assert "past the end" in result.text_content()
    assert "2 lines" in result.text_content()


async def test_read_empty_file_still_reports_empty(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    result = await ReadTool().run({"path": "empty.txt"}, ctx)
    assert not result.is_error
    assert result.text_content().strip() == "(empty file)"


async def test_glob_skips_dependency_directories(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "vendored.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("y = 1\n", encoding="utf-8")

    found = await GlobTool().run({"pattern": "**/*.py"}, ctx)
    assert "app.py" in found.text_content()
    assert "vendored.py" not in found.text_content()

    # Naming the directory explicitly still searches it.
    targeted = await GlobTool().run({"pattern": "**/*.py", "path": ".venv"}, ctx)
    assert "vendored.py" in targeted.text_content()


async def test_bash_reports_exit_code_marker(tmp_path: Path) -> None:
    result = await BashTool().run({"command": "exit 7"}, _ctx(tmp_path))
    assert "[exit code: 7]" in result.text_content()


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


async def test_read_returns_an_image_block(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (tmp_path / "dot.png").write_bytes(_PNG)
    result = await ReadTool().run({"path": "dot.png"}, ctx)
    assert not result.is_error
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, ImageBlock)
    assert block.media_type == "image/png"
    assert base64.b64decode(block.data) == _PNG


def test_remember_is_registered_only_with_a_memory_store(tmp_path: Path) -> None:
    from cli_agent.context.memory import MemoryStore
    from cli_agent.context.skills import SkillLoader
    from cli_agent.tools.builtin import build_builtin_registry

    plain = build_builtin_registry(SkillLoader([]))
    assert plain.get("Remember") is None

    store = MemoryStore(tmp_path / "memory")
    with_memory = build_builtin_registry(SkillLoader([]), memory=store)
    remember = with_memory.get("Remember")
    assert remember is not None
    # The description carries the strict admission criteria.
    assert "rarely" in remember.spec.description
    assert "Do not save task state" in remember.spec.description
