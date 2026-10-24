from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli_agent.context.skills import (
    MAX_SKILL_BODY_BYTES,
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_NAME_CHARS,
    SkillLoader,
    _read_skill_text,
)
from cli_agent.tools.builtin.skills import LoadSkillTool


def _skill(root: Path, name: str, description: str, body: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_project_skill_overrides_global(tmp_path: Path) -> None:
    global_root = tmp_path / "global"
    project_root = tmp_path / "project"
    _skill(global_root, "deploy", "global deploy", "global body")
    _skill(project_root, "deploy", "project deploy", "project body")

    loader = SkillLoader([(global_root, "global"), (project_root, "project")])
    skills = loader.catalog()
    assert len(skills) == 1
    assert skills[0].description == "project deploy"
    assert loader.load("deploy") == "project body"
    assert loader.catalog_lines() == ["- deploy [project]: project deploy"]


def test_missing_skill_raises(tmp_path: Path) -> None:
    loader = SkillLoader([(tmp_path, "global")])
    with pytest.raises(KeyError):
        loader.load("missing")


async def test_load_skill_tool_reports_catalog(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _skill(root, "alpha", "first skill", "alpha body")
    loader = SkillLoader([(root, "global")])
    tool = LoadSkillTool(loader)
    result = await tool.run({"name": "alpha"}, ctx=None)  # type: ignore[arg-type]
    assert result.text_content() == "alpha body"

    missing = await tool.run({"name": "beta"}, ctx=None)  # type: ignore[arg-type]
    assert missing.is_error
    assert "alpha" in missing.text_content()


def test_skill_description_is_single_line_and_capped(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    directory = root / "long"
    directory.mkdir(parents=True)
    description = "word   " * (MAX_SKILL_DESCRIPTION_CHARS + 100)
    (directory / "SKILL.md").write_text(
        f"---\nname: long\ndescription: {description}\n---\n\nbody\n",
        encoding="utf-8",
    )
    skill = SkillLoader([(root, "project")]).catalog()[0]
    assert "\n" not in skill.description
    assert "  " not in skill.description
    assert len(skill.description) <= MAX_SKILL_DESCRIPTION_CHARS + 3


def test_skill_name_is_single_line_and_capped(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    directory = root / "weird"
    directory.mkdir(parents=True)
    name = "n" * (MAX_SKILL_NAME_CHARS + 100)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\nbody\n",
        encoding="utf-8",
    )
    skill = SkillLoader([(root, "project")]).catalog()[0]
    assert len(skill.name) == MAX_SKILL_NAME_CHARS


def test_skill_body_is_capped(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _skill(root, "big", "big body", "x" * (MAX_SKILL_BODY_BYTES + 1_000))
    loader = SkillLoader([(root, "project")])
    body = loader.load("big")
    assert body.endswith("[skill truncated] ...")
    assert len(body.encode("utf-8")) <= MAX_SKILL_BODY_BYTES


def test_skill_file_larger_than_cap_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _skill(root, "huge", "huge file", "y" * (MAX_SKILL_FILE_BYTES + 10_000))
    loader = SkillLoader([(root, "project")])
    assert loader.catalog()[0].name == "huge"
    assert loader.load("huge").endswith("[skill truncated] ...")


def test_symlink_skill_is_skipped(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _skill(outside, "bad", "outside", "outside body")
    root = tmp_path / "skills"
    root.mkdir()
    try:
        os.symlink(outside / "bad", root / "bad", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available")

    loader = SkillLoader([(root, "project")])
    assert loader.catalog() == ()
    with pytest.raises(KeyError):
        loader.load("bad")


async def test_load_skill_tool_reports_read_error(tmp_path: Path) -> None:
    class BrokenSource:
        def load(self, name: str) -> str:
            raise OSError("permission denied")

        def catalog_lines(self) -> list[str]:
            return []

    result = await LoadSkillTool(BrokenSource()).run(
        {"name": "alpha"}, ctx=None  # type: ignore[arg-type]
    )
    assert result.is_error
    assert "could not read skill" in result.text_content()


def test_symlink_skill_root_is_skipped(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _skill(outside, "bad", "outside", "outside body")
    link = tmp_path / "linked-skills"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available")

    loader = SkillLoader([(link, "project")])
    assert loader.catalog() == ()


def test_symlink_skill_file_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    directory = root / "bad"
    directory.mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("TOP SECRET", encoding="utf-8")
    try:
        os.symlink(secret, directory / "SKILL.md")
    except OSError:
        pytest.skip("symlinks are not available")

    loader = SkillLoader([(root, "project")])
    assert loader.catalog() == ()


async def test_load_revalidates_swapped_symlink(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _skill(root, "alpha", "first", "alpha body")
    secret = tmp_path / "secret.md"
    secret.write_text("TOP SECRET", encoding="utf-8")
    loader = SkillLoader([(root, "project")])
    assert loader.catalog()[0].name == "alpha"

    skill_file = root / "alpha" / "SKILL.md"
    skill_file.unlink()
    try:
        os.symlink(secret, skill_file)
    except OSError:
        pytest.skip("symlinks are not available")

    result = await LoadSkillTool(loader).run(
        {"name": "alpha"}, ctx=None  # type: ignore[arg-type]
    )
    assert result.is_error
    assert "TOP SECRET" not in result.text_content()


def test_fifo_skill_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    directory = root / "fifo"
    directory.mkdir(parents=True)
    try:
        os.mkfifo(directory / "SKILL.md")
    except (AttributeError, OSError):
        pytest.skip("fifo is not available")

    loader = SkillLoader([(root, "project")])
    assert loader.catalog() == ()


def test_control_characters_are_stripped(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    directory = root / "weird"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        "name: bad\x00name\n"
        "description: bad\x1b[31mtext‮​\x7f end\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    skill = SkillLoader([(root, "project")]).catalog()[0]
    for char in ("\x00", "\x1b", "‮", "​", "\x7f"):
        assert char not in skill.name
        assert char not in skill.description


async def test_catalog_error_listing_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    for index in range(60):
        _skill(root, f"skill-{index}", f"description {index}", "body")
    loader = SkillLoader([(root, "project")])
    lines = loader.catalog_lines(limit=50)
    assert len(lines) == 51
    assert "10 more skills" in lines[-1]

    result = await LoadSkillTool(loader).run(
        {"name": "missing"}, ctx=None  # type: ignore[arg-type]
    )
    assert result.is_error
    assert "more skills" in result.text_content()


def test_read_skill_text_file_cap(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_bytes(b"a" * MAX_SKILL_FILE_BYTES)
    _text, truncated = _read_skill_text(path)
    assert not truncated

    path.write_bytes(b"a" * (MAX_SKILL_FILE_BYTES + 1))
    text, truncated = _read_skill_text(path)
    assert truncated
    assert len(text.encode("utf-8")) <= MAX_SKILL_FILE_BYTES


def test_unclosed_frontmatter_is_not_body_derived(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    directory = root / "real-directory"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: real-name\n"
        + "body line\n" * 50
        + "name: evil-name\n",
        encoding="utf-8",
    )
    loader = SkillLoader([(root, "project")])
    skill = loader.catalog()[0]
    assert skill.name == "real-directory"
    assert "evil-name" not in loader.load(skill.name)


async def test_real_loader_read_error(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _skill(root, "alpha", "first", "alpha body")
    loader = SkillLoader([(root, "project")])
    assert loader.catalog()
    (root / "alpha" / "SKILL.md").unlink()

    result = await LoadSkillTool(loader).run(
        {"name": "alpha"}, ctx=None  # type: ignore[arg-type]
    )
    assert result.is_error
    assert "could not read skill" in result.text_content()


def test_same_root_name_collision_keeps_first(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _skill(root, "first", "first description", "first body")
    _skill(root, "second", "second description", "second body")
    # Rewrite the second skill's name to collide with the first.
    (root / "second" / "SKILL.md").write_text(
        "---\nname: first\ndescription: second description\n---\n\nsecond body\n",
        encoding="utf-8",
    )
    loader = SkillLoader([(root, "project")])
    skills = loader.catalog()
    assert len(skills) == 1
    assert skills[0].description == "first description"
