from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from cli_agent.entry import main
from cli_agent.events import AgentError, AgentTextDelta, AgentTurnFinished
from cli_agent.messages import Usage


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }


def test_help(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["--help"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "-p, --print" in result.output
    assert "--permission-mode" in result.output
    assert "--extended-thinking" in result.output


def test_version(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["--version"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_missing_key_fails_clearly(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main, ["--provider", "kimi", "-p", "hi"], env=_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "no API key" in result.output


def test_resume_without_sessions_fails(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["-c"], env=_env(tmp_path))
    assert result.exit_code == 1
    assert "no saved sessions" in result.output


def test_print_and_positional_conflict(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["-p", "one", "two"], env=_env(tmp_path))
    assert result.exit_code != 0
    assert "not both" in result.output


def test_print_mode_wiring(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr("cli_agent.entry.build_runtime", lambda options: bundle)
    result = CliRunner().invoke(main, ["-p", "hi"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "hello" in result.output


def test_print_mode_error_exit_code(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr("cli_agent.entry.build_runtime", lambda options: bundle)
    result = CliRunner().invoke(main, ["-p", "hi"], env=_env(tmp_path))
    assert result.exit_code == 2


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

    monkeypatch.setattr("cli_agent.entry.build_runtime", fake_build)
    result = CliRunner().invoke(
        main,
        ["-p", "hi", "--extended-thinking", "--thinking-budget", "2048"],
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
    monkeypatch.setattr("cli_agent.entry.build_runtime", lambda options: bundle)
    result = CliRunner().invoke(
        main, ["-p", "hi", "--thinking"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    combined = result.output + (getattr(result, "stderr", "") or "")
    assert "only controls display" in combined
