from __future__ import annotations

from io import StringIO

from rich.console import Console

from cli_agent.cli.render import PrintRenderer, Renderer
from cli_agent.events import AgentRetry, AgentTextDelta, AgentTurnFinished
from cli_agent.messages import Usage


def _console() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, width=100), output


def test_welcome_shows_the_permission_mode() -> None:
    console, output = _console()
    renderer = Renderer(console=console)
    renderer.welcome(model="m", provider="p", session_id="s", permission_mode="accept-edits")
    assert "mode: accept-edits" in output.getvalue()

    console, output = _console()
    renderer = Renderer(console=console)
    renderer.welcome(model="m", provider="p", session_id="s", permission_mode="bypass")
    text = output.getvalue()
    assert "mode: bypass" in text
    assert "no approval prompts" in text


def test_repl_reasoning_has_a_visible_header_and_separator() -> None:
    console, output = _console()
    renderer = Renderer(console=console, show_reasoning=True)
    renderer.render(AgentTextDelta("considering the options", reasoning=True))
    renderer.render(AgentTextDelta("the answer is 42"))
    renderer.render(AgentTurnFinished("end_turn", Usage()))
    text = output.getvalue()
    assert "thinking..." in text
    assert text.index("considering the options") < text.index("the answer is 42")


def test_repl_can_hide_reasoning() -> None:
    console, output = _console()
    renderer = Renderer(console=console, show_reasoning=False)
    renderer.render(AgentTextDelta("private reasoning", reasoning=True))
    renderer.render(AgentTextDelta("the answer"))
    renderer.render(AgentTurnFinished("end_turn", Usage()))
    text = output.getvalue()
    assert "private reasoning" not in text
    assert "thinking..." not in text
    assert "the answer" in text


def test_print_reasoning_goes_to_stderr(capsys) -> None:
    renderer = PrintRenderer(show_reasoning=True)
    renderer.render(AgentTextDelta("thinking text", reasoning=True))
    renderer.render(AgentTextDelta("final answer"))
    renderer.render(AgentTurnFinished("end_turn", Usage()))
    captured = capsys.readouterr()
    assert "thinking text" in captured.err
    assert "thinking text" not in captured.out
    assert "final answer" in captured.out


def test_print_hides_reasoning_by_default(capsys) -> None:
    renderer = PrintRenderer()
    renderer.render(AgentTextDelta("hidden", reasoning=True))
    renderer.render(AgentTextDelta("answer"))
    renderer.render(AgentTurnFinished("end_turn", Usage()))
    captured = capsys.readouterr()
    assert "hidden" not in captured.out
    assert "hidden" not in captured.err
    assert "answer" in captured.out


def test_repl_shows_retry_and_stop_reason() -> None:
    console, output = _console()
    renderer = Renderer(console=console)
    renderer.render(AgentTextDelta("first attempt"))
    renderer.render(AgentRetry(1, 3, "retryable", "rate limited", partial=True))
    renderer.render(AgentTextDelta("second attempt"))
    renderer.render(AgentTurnFinished("max_turns", Usage()))
    text = output.getvalue()
    assert "retrying 1/3" in text
    assert "abandoned attempt" in text
    assert "stopped: max_turns" in text


def test_print_reports_retry_on_stderr(capsys) -> None:
    renderer = PrintRenderer()
    renderer.render(AgentTextDelta("first attempt"))
    renderer.render(AgentRetry(2, 3, "retryable", "overloaded", partial=True))
    renderer.render(AgentTextDelta("final answer"))
    renderer.render(AgentTurnFinished("end_turn", Usage()))
    captured = capsys.readouterr()
    assert "retrying 2/3" in captured.err
    assert "retrying" not in captured.out
    assert "final answer" in captured.out


def test_print_marks_failed_stop_reasons(capsys) -> None:
    for reason in ("refusal", "error", "max_turns"):
        renderer = PrintRenderer()
        renderer.render(AgentTextDelta("partial"))
        renderer.render(AgentTurnFinished(reason, Usage()))
        captured = capsys.readouterr()
        assert renderer.failed, reason
        assert f"stopped: {reason}" in captured.err

    renderer = PrintRenderer()
    renderer.render(AgentTurnFinished("end_turn", Usage()))
    assert not renderer.failed


def test_history_renders_the_resumed_conversation() -> None:
    from cli_agent.messages import TextBlock, ToolUseBlock, assistant_message, user_message

    messages = (
        user_message("earlier question"),
        assistant_message(
            [TextBlock("earlier answer"), ToolUseBlock("t1", "Bash", {"command": "ls"})]
        ),
        user_message("next"),
    )
    console, output = _console()
    Renderer(console=console).history(messages)
    text = output.getvalue()
    assert "resumed history" in text
    assert "earlier question" in text
    assert "earlier answer" in text
    assert "Bash" in text
    assert "end of history" in text

    console, output = _console()
    Renderer(console=console).history(messages, limit=1)
    assert "2 earlier message(s) hidden" in output.getvalue()
