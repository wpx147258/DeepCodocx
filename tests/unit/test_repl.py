from __future__ import annotations

from types import SimpleNamespace

from cli_agent.cli.repl import run_repl
from cli_agent.messages import TextBlock, assistant_message, user_message


class _Runtime:
    permission_mode = "accept-edits"

    def __init__(self, messages=()) -> None:
        self.messages = messages
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _bundle(runtime: _Runtime) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=runtime,
        profile=SimpleNamespace(model="m", provider_name="p"),
        session_id="s",
        resumed=True,
        degraded=False,
    )


async def test_run_repl_shows_the_previous_conversation(monkeypatch, capsys) -> None:
    runtime = _Runtime(
        (
            user_message("earlier question"),
            assistant_message([TextBlock("earlier answer")]),
        )
    )
    monkeypatch.setattr("cli_agent.cli.repl.read_user_input", lambda: None)

    code = await run_repl(_bundle(runtime))

    out = capsys.readouterr().out
    assert code == 0
    assert "resumed history" in out
    assert "earlier question" in out
    assert "earlier answer" in out
    assert runtime.closed


async def test_run_repl_notes_an_empty_resume(monkeypatch, capsys) -> None:
    runtime = _Runtime(())
    monkeypatch.setattr("cli_agent.cli.repl.read_user_input", lambda: None)

    await run_repl(_bundle(runtime))

    assert "Resumed an empty session." in capsys.readouterr().out


async def test_run_repl_notes_a_fork(monkeypatch, capsys) -> None:
    runtime = _Runtime(())
    bundle = _bundle(runtime)
    bundle.forked_from = "sess_old"
    monkeypatch.setattr("cli_agent.cli.repl.read_user_input", lambda: None)

    await run_repl(bundle)

    assert "Forked from session sess_old." in capsys.readouterr().out
