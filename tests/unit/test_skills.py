from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent.context.skills import SkillLoader
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
    assert loader.catalog_lines() == ["- deploy: project deploy"]


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
