from __future__ import annotations

import json

from dcx.cli.render import JsonRenderer
from dcx.events import (
    AgentContextEvent,
    AgentError,
    AgentNotice,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnFinished,
    AgentUsage,
)
from dcx.messages import TextBlock, Usage


def _renderer(*, stream: bool, show_reasoning: bool = False) -> JsonRenderer:
    return JsonRenderer(
        session_id="sess_test",
        provider="mock",
        model="mock-model",
        stream=stream,
        show_reasoning=show_reasoning,
    )


def _events() -> list:
    return [
        AgentTextDelta("hello"),
        AgentToolStarted("t1", "Bash", {"command": "echo"}),
        AgentToolFinished("t1", "Bash", (TextBlock("ok"),), False),
        AgentUsage(Usage(1, 2), Usage(1, 2)),
        AgentTurnFinished("end_turn", Usage(1, 2)),
    ]


def test_jsonl_emits_ordered_events(capsys) -> None:
    renderer = _renderer(stream=True)
    renderer.start()
    for event in _events():
        renderer.render(event)
    renderer.finish()

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["type"] for line in lines] == [
        "session_start",
        "text",
        "tool_started",
        "tool_finished",
        "usage",
        "result",
    ]
    result = lines[-1]
    assert result["session_id"] == "sess_test"
    assert result["stop_reason"] == "end_turn"
    assert result["is_complete"] is True
    assert result["text"] == "hello"
    assert result["tool_calls"][0]["content"] == "ok"


def test_json_emits_one_object(capsys) -> None:
    renderer = _renderer(stream=False)
    renderer.start()
    for event in _events():
        renderer.render(event)
    renderer.finish()

    output = capsys.readouterr().out
    assert len(output.strip().splitlines()) == 1
    result = json.loads(output)
    assert result["type"] == "result"
    assert result["text"] == "hello"
    assert result["tool_calls"][0]["name"] == "Bash"
    assert result["usage"] == {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def test_hidden_reasoning_is_not_emitted(capsys) -> None:
    renderer = _renderer(stream=False, show_reasoning=False)
    renderer.render(AgentTextDelta("secret", reasoning=True))
    renderer.render(AgentTextDelta("answer"))
    renderer.render(AgentTurnFinished("end_turn", Usage()))
    renderer.finish()

    result = json.loads(capsys.readouterr().out)
    assert result["text"] == "answer"
    assert "reasoning" not in result


def test_visible_reasoning_is_emitted(capsys) -> None:
    renderer = _renderer(stream=True, show_reasoning=True)
    renderer.start()
    renderer.render(AgentTextDelta("think", reasoning=True))
    renderer.render(AgentTurnFinished("end_turn", Usage()))
    renderer.finish()

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(line["type"] == "reasoning" and line["text"] == "think" for line in lines)
    assert lines[-1]["reasoning"] == "think"


def test_notice_context_and_error_are_data(capsys) -> None:
    renderer = _renderer(stream=False)
    renderer.render(AgentNotice("output truncated"))
    renderer.render(AgentContextEvent("compact", "summarized 4 messages"))
    renderer.render(AgentError("bad", "fatal", 500))
    renderer.render(AgentTurnFinished("error", Usage()))
    renderer.finish()

    result = json.loads(capsys.readouterr().out)
    assert result["notices"] == ["output truncated"]
    assert result["context_events"] == [
        {"kind": "compact", "detail": "summarized 4 messages"}
    ]
    assert result["is_error"] is True
    assert result["error"]["kind"] == "fatal"
    assert result["is_complete"] is False


def test_fatal_exception_emits_result(capsys) -> None:
    renderer = _renderer(stream=False)
    renderer.fatal(RuntimeError("boom"))

    result = json.loads(capsys.readouterr().out)
    assert result["is_error"] is True
    assert result["stop_reason"] == "error"
    assert result["error"]["kind"] == "fatal"
