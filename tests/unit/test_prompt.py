from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from dcx.context.prompts import PromptBuilder, PromptContext, load_project_instructions
from dcx.context.skills import Skill
from dcx.providers.base import ModelProfile
from dcx.tools.base import ToolSpec


def _profile() -> ModelProfile:
    return ModelProfile(
        provider_name="deepseek",
        provider_kind="anthropic_compat",
        model="deepseek-flash[1M]",
        context_window=1_000_000,
        max_output_tokens=8_192,
        supports_thinking=True,
    )


def _tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            "Read", "Read a text file.", {"type": "object"}, category="read", path_arg="path"
        ),
        ToolSpec(
            "Write", "Write a file.", {"type": "object"}, category="write", path_arg="path"
        ),
        ToolSpec("Bash", "Run a shell command.", {"type": "object"}, category="shell"),
        ToolSpec("Glob", "Find files.", {"type": "object"}, category="read"),
        ToolSpec("Grep", "Search file contents.", {"type": "object"}, category="read"),
    )


def _context(tmp_path: Path) -> PromptContext:
    return PromptContext(
        cwd=tmp_path,
        profile=_profile(),
        tools=_tools(),
        skills=(),
        project_instructions="",
        active_request="hi",
    )


def _text(context: PromptContext) -> str:
    return "\n\n".join(section.text for section in PromptBuilder().build(context))


def test_prompt_has_modular_sections_and_actual_identity(tmp_path: Path) -> None:
    sections = PromptBuilder().build(_context(tmp_path))
    names = [section.name for section in sections]
    assert names[:5] == ["identity", "workspace", "tools", "file-operations", "shell"]
    text = "\n\n".join(section.text for section in sections)
    assert "cli-agent" in text
    assert str(tmp_path) in text
    assert "deepseek provider" in text
    assert "deepseek-flash[1M] model" in text


def test_prompt_lists_only_actual_tools(tmp_path: Path) -> None:
    text = _text(_context(tmp_path))
    assert "Read [read]: Read a text file." in text
    assert "Bash [shell]: Run a shell command." in text
    assert "Use Glob, not shell find" in text
    assert "Use Grep, not shell grep" in text
    assert "[exit code: N]" in text
    assert "@src/app.py" in text
    assert '@"..."' in text
    assert "trailing slash marks a directory" in text
    assert "Treat a path without a trailing slash as a file" in text
    assert "Do not claim to have inspected a file before reading it" in text


def test_prompt_does_not_claim_unimplemented_capabilities(tmp_path: Path) -> None:
    text = _text(_context(tmp_path))
    for unsupported in (
        "DeepSeek Harness",
        "web_search",
        "web_fetch",
        "subagent",
        "workflow",
        "background job",
        "job_output",
        "create_goal",
        "update_goal",
        "present",
        "GUI",
        "HMR",
        "fs-observation-policy",
    ):
        assert unsupported not in text


def test_compaction_section_is_conditional(tmp_path: Path) -> None:
    context = _context(tmp_path)
    assert "compaction" not in [section.name for section in PromptBuilder().build(context)]
    active = replace(context, compaction_active=True)
    sections = PromptBuilder().build(active)
    assert "compaction" in [section.name for section in sections]


def test_project_memory_and_skills_sections(tmp_path: Path) -> None:
    context = replace(
        _context(tmp_path),
        project_instructions="Use tabs.",
        memory_enabled=True,
        skills=(Skill("deploy", "Deploy the app", tmp_path / "SKILL.md", "global"),),
    )
    sections = PromptBuilder().build(context)
    names = [section.name for section in sections]
    assert "project" in names
    assert "memory" in names
    assert "skills" in names
    text = "\n\n".join(section.text for section in sections)
    assert "Use tabs." in text
    assert "Use tabs." in text
    assert "deploy [global]: Deploy the app" in text
    # Saved records are injected into the current user message, not the system
    # prompt, so the cacheable prefix stays stable.
    assert "Memory catalog" not in text


def test_agent_name_is_configurable(tmp_path: Path) -> None:
    context = replace(_context(tmp_path), agent_name="custom-agent")
    assert "custom-agent" in _text(context)


def test_load_project_instructions_and_includes(tmp_path: Path) -> None:
    (tmp_path / "extra.md").write_text("Included rule.", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "@extra.md\nLocal rule.", encoding="utf-8"
    )
    text = load_project_instructions(tmp_path)
    assert "Included rule." in text
    assert "Local rule." in text


def test_memory_guidance_appears_when_memory_is_enabled(tmp_path: Path) -> None:
    context = replace(_context(tmp_path), memory_enabled=True)
    sections = {section.name: section.text for section in PromptBuilder().build(context)}
    assert "memory" in sections
    assert "call Remember" in sections["memory"]
    assert "Never save task state" in sections["memory"]
    assert "Most turns need no call" in sections["memory"]


def test_memory_section_is_absent_when_memory_is_disabled(tmp_path: Path) -> None:
    context = replace(_context(tmp_path), memory_enabled=False)
    assert "memory" not in [section.name for section in PromptBuilder().build(context)]


def test_project_include_cannot_escape_workspace(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    (project / "AGENTS.md").write_text(
        "@../secret.txt\n@/etc/passwd\nLocal rule.\n", encoding="utf-8"
    )
    text = load_project_instructions(project)
    assert "TOP SECRET" not in text
    assert "root:" not in text
    assert "include denied" in text
    assert "Local rule." in text


def test_global_include_is_limited_to_config_dir(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "notes.md").write_text("Global notes", encoding="utf-8")
    (global_dir / "AGENTS.md").write_text(
        "@notes.md\n@../outside.md\n", encoding="utf-8"
    )
    (tmp_path / "outside.md").write_text("Outside secret", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()

    text = load_project_instructions(project, global_dir / "AGENTS.md")
    assert "Global notes" in text
    assert "Outside secret" not in text
    assert "include denied" in text
