from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from dcx.entry import main
from dcx.events import (
    AgentError,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnFinished,
    AgentUsage,
)
from dcx.messages import TextBlock, Usage


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }


def test_help(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["--help"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "run" in result.output
    assert "--permission-mode" in result.output
    assert "--extended-thinking" in result.output


def test_version(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["--version"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_missing_key_fails_clearly(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main, ["run", "--provider", "kimi", "hi"], env=_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "no API key" in result.output


def test_resume_without_sessions_fails(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["-c"], env=_env(tmp_path))
    assert result.exit_code == 1
    assert "no saved sessions" in result.output


def test_run_requires_prompt(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["run"], env=_env(tmp_path))
    assert result.exit_code != 0
    assert "requires a prompt" in result.output


def test_run_help(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["run", "--help"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "--output-format" in result.output
    assert "PROMPT" in result.output


def test_one_shot_wiring(tmp_path: Path, monkeypatch) -> None:
    class FakeRuntime:
        async def run_turn(self, text: str):
            assert text == "hi"
            yield AgentTextDelta("hello")
            yield AgentTurnFinished("end_turn", Usage())

        async def close(self) -> None:
            return None

    bundle = SimpleNamespace(
        runtime=FakeRuntime(),
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        degraded=False,
    )
    monkeypatch.setattr("dcx.entry.build_runtime", lambda options: bundle)
    result = CliRunner().invoke(main, ["run", "hi"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "hello" in result.output


def test_one_shot_error_exit_code(tmp_path: Path, monkeypatch) -> None:
    class FakeRuntime:
        async def run_turn(self, text: str):
            yield AgentError("bad", "fatal")

        async def close(self) -> None:
            return None

    bundle = SimpleNamespace(
        runtime=FakeRuntime(),
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        degraded=False,
    )
    monkeypatch.setattr("dcx.entry.build_runtime", lambda options: bundle)
    result = CliRunner().invoke(main, ["run", "hi"], env=_env(tmp_path))
    assert result.exit_code == 2


def test_print_mode_refusal_exit_code(tmp_path: Path, monkeypatch) -> None:
    class FakeRuntime:
        async def run_turn(self, text: str):
            yield AgentTextDelta("I can't help with that.")
            yield AgentTurnFinished("refusal", Usage())

        async def close(self) -> None:
            return None

    bundle = SimpleNamespace(
        runtime=FakeRuntime(),
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        degraded=False,
    )
    monkeypatch.setattr("dcx.entry.build_runtime", lambda options: bundle)
    result = CliRunner().invoke(main, ["run", "hi"], env=_env(tmp_path))
    assert result.exit_code == 2
    combined = result.output + (getattr(result, "stderr", "") or "")
    assert "refusal" in combined


def test_thinking_flags_reach_build_options(tmp_path: Path, monkeypatch) -> None:
    class FakeRuntime:
        async def run_turn(self, text: str):
            yield AgentTextDelta("ok")
            yield AgentTurnFinished("end_turn", Usage())

        async def close(self) -> None:
            return None

    bundle = SimpleNamespace(
        runtime=FakeRuntime(),
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        degraded=False,
    )
    captured = {}

    def fake_build(options):
        captured["options"] = options
        return bundle

    monkeypatch.setattr("dcx.entry.build_runtime", fake_build)
    result = CliRunner().invoke(
        main,
        ["run", "--extended-thinking", "--thinking-budget", "2048", "hi"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["options"].thinking_enabled is True
    assert captured["options"].thinking_budget == 2048


def test_thinking_display_without_request_warns(tmp_path: Path, monkeypatch) -> None:
    class FakeRuntime:
        async def run_turn(self, text: str):
            yield AgentTextDelta("ok")
            yield AgentTurnFinished("end_turn", Usage())

        async def close(self) -> None:
            return None

    bundle = SimpleNamespace(
        runtime=FakeRuntime(),
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        degraded=False,
    )
    monkeypatch.setattr("dcx.entry.build_runtime", lambda options: bundle)
    result = CliRunner().invoke(
        main, ["run", "--thinking", "hi"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    combined = result.output + (getattr(result, "stderr", "") or "")
    assert "only controls display" in combined


def test_output_format_jsonl(tmp_path: Path, monkeypatch) -> None:
    class FakeRuntime:
        async def run_turn(self, text: str):
            yield AgentTextDelta("hello")
            yield AgentToolStarted("t1", "Bash", {"command": "echo"})
            yield AgentToolFinished("t1", "Bash", (TextBlock("ok"),), False)
            yield AgentUsage(Usage(1, 2), Usage(1, 2))
            yield AgentTurnFinished("end_turn", Usage(1, 2))

        async def close(self) -> None:
            return None

    bundle = SimpleNamespace(
        runtime=FakeRuntime(),
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        resumed=False,
        degraded=False,
    )
    monkeypatch.setattr("dcx.entry.build_runtime", lambda options: bundle)
    result = CliRunner().invoke(
        main, ["run", "--output-format", "jsonl", "hi"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    lines = [json.loads(line) for line in result.output.splitlines()]
    assert [line["type"] for line in lines] == [
        "session_start",
        "text",
        "tool_started",
        "tool_finished",
        "usage",
        "result",
    ]
    assert lines[-1]["text"] == "hello"
    assert lines[-1]["stop_reason"] == "end_turn"


def test_output_format_json_single_object(tmp_path: Path, monkeypatch) -> None:
    class FakeRuntime:
        async def run_turn(self, text: str):
            yield AgentTextDelta("hello")
            yield AgentTurnFinished("end_turn", Usage(1, 2))

        async def close(self) -> None:
            return None

    bundle = SimpleNamespace(
        runtime=FakeRuntime(),
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        resumed=False,
        degraded=False,
    )
    monkeypatch.setattr("dcx.entry.build_runtime", lambda options: bundle)
    result = CliRunner().invoke(
        main, ["run", "--output-format", "json", "hi"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    assert len(result.output.strip().splitlines()) == 1
    payload = json.loads(result.output)
    assert payload["type"] == "result"
    assert payload["text"] == "hello"


def test_output_format_requires_prompt(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["run", "--output-format", "json"], env=_env(tmp_path))
    assert result.exit_code != 0
    assert "requires a prompt" in result.output


def test_json_error_event_exits_two(tmp_path: Path, monkeypatch) -> None:
    class FakeRuntime:
        async def run_turn(self, text: str):
            yield AgentError("bad", "fatal", 500)
            yield AgentTurnFinished("error", Usage())

        async def close(self) -> None:
            return None

    bundle = SimpleNamespace(
        runtime=FakeRuntime(),
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        resumed=False,
        degraded=False,
    )
    monkeypatch.setattr("dcx.entry.build_runtime", lambda options: bundle)
    result = CliRunner().invoke(
        main, ["run", "--output-format", "jsonl", "hi"], env=_env(tmp_path)
    )
    assert result.exit_code == 2
    lines = [json.loads(line) for line in result.output.splitlines()]
    assert any(line["type"] == "error" for line in lines)
    assert lines[-1]["is_error"] is True


def test_unexpected_exception_goes_to_stderr(tmp_path: Path, monkeypatch) -> None:
    class FakeRuntime:
        async def run_turn(self, text: str):
            raise RuntimeError("boom")
            yield  # pragma: no cover

        async def close(self) -> None:
            return None

    bundle = SimpleNamespace(
        runtime=FakeRuntime(),
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        resumed=False,
        degraded=False,
    )
    monkeypatch.setattr("dcx.entry.build_runtime", lambda options: bundle)
    result = CliRunner().invoke(main, ["run", "hi"], env=_env(tmp_path))
    assert result.exit_code == 2
    assert result.stdout.strip() == ""
    assert "boom" in result.stderr


def test_incomplete_turn_exits_two(tmp_path: Path, monkeypatch) -> None:
    class FakeRuntime:
        async def run_turn(self, text: str):
            yield AgentTurnFinished("max_turns", Usage())

        async def close(self) -> None:
            return None

    bundle = SimpleNamespace(
        runtime=FakeRuntime(),
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        resumed=False,
        degraded=False,
    )
    monkeypatch.setattr("dcx.entry.build_runtime", lambda options: bundle)
    result = CliRunner().invoke(main, ["run", "hi"], env=_env(tmp_path))
    assert result.exit_code == 2


def test_global_options_apply_to_run(tmp_path: Path, monkeypatch) -> None:
    class FakeRuntime:
        async def run_turn(self, text: str):
            yield AgentTextDelta("ok")
            yield AgentTurnFinished("end_turn", Usage())

        async def close(self) -> None:
            return None

    bundle = SimpleNamespace(
        runtime=FakeRuntime(),
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        resumed=False,
        degraded=False,
    )
    captured = {}

    def fake_build(options):
        captured["options"] = options
        return bundle

    monkeypatch.setattr("dcx.entry.build_runtime", fake_build)
    result = CliRunner().invoke(
        main, ["--provider", "deepseek", "run", "hi"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    assert captured["options"].provider == "deepseek"


def _bundle() -> SimpleNamespace:
    class FakeRuntime:
        async def run_turn(self, text: str):
            yield AgentTextDelta("ok")
            yield AgentTurnFinished("end_turn", Usage())

        async def close(self) -> None:
            return None

    return SimpleNamespace(
        runtime=FakeRuntime(),
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        resumed=False,
        degraded=False,
    )


def test_fork_requires_a_resume_target(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["run", "--fork", "hi"], env=_env(tmp_path))
    assert result.exit_code == 1
    assert "--fork requires" in result.output


def test_fork_flag_reaches_build_options(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle()
    captured = {}

    def fake_build(options):
        captured["options"] = options
        return bundle

    monkeypatch.setattr("dcx.entry.build_runtime", fake_build)
    result = CliRunner().invoke(
        main, ["run", "-c", "--fork", "hi"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    assert captured["options"].fork is True
    assert captured["options"].resume_id == "latest"


@pytest.mark.parametrize(
    "args",
    [
        ["--max-turns", "0", "run", "hi"],
        ["run", "--max-turns", "0", "hi"],
        ["--max-turns", "-1", "run", "hi"],
        ["run", "--max-turns", "-3", "hi"],
    ],
)
def test_max_turns_rejects_non_positive(tmp_path: Path, args: list[str]) -> None:
    result = CliRunner().invoke(main, args, env=_env(tmp_path))
    assert result.exit_code == 2
    assert "not in the range" in result.output


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--max-turns", "1", "run", "hi"], 1),
        (["run", "--max-turns", "2", "hi"], 2),
        (["--max-turns", "5", "run", "--max-turns", "7", "hi"], 7),
    ],
)
def test_max_turns_normal_values_pass(
    tmp_path: Path, monkeypatch, args: list[str], expected: int
) -> None:
    bundle = _bundle()
    captured = {}

    def fake_build(options):
        captured["options"] = options
        return bundle

    monkeypatch.setattr("dcx.entry.build_runtime", fake_build)
    result = CliRunner().invoke(main, args, env=_env(tmp_path))
    assert result.exit_code == 0
    assert captured["options"].max_turns == expected
