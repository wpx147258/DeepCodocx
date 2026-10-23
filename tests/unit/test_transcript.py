from __future__ import annotations

from cli_agent.messages import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    assistant_message,
    now_iso,
    user_message,
)
from cli_agent.transcript import MISSING_TOOL_RESULT, Transcript


def test_orphan_tool_use_at_tail_gets_synthetic_result() -> None:
    assistant = assistant_message([ToolUseBlock("t1", "Echo", {"text": "x"})])
    view = Transcript([user_message("hi"), assistant]).for_request()
    assert [message.role for message in view] == ["user", "assistant", "user"]
    result = view[-1].tool_results()[0]
    assert result.tool_use_id == "t1"
    assert result.is_error
    assert result.content[0].text == MISSING_TOOL_RESULT


def test_orphan_tool_result_is_dropped() -> None:
    orphan = Message(
        id="m1",
        role="user",
        content=(ToolResultBlock("missing", (TextBlock("orphan"),), is_error=True),),
        created_at=now_iso(),
    )
    view = Transcript([orphan, user_message("keep me")]).for_request()
    assert view[0].role == "user"
    assert all(not message.tool_results() for message in view)
    assert "keep me" in view[0].text()


def test_matching_tool_pair_is_preserved() -> None:
    assistant = assistant_message([ToolUseBlock("t1", "Echo", {"text": "x"})])
    result = Message(
        id="r1",
        role="user",
        content=(ToolResultBlock("t1", (TextBlock("ok"),)),),
        created_at=now_iso(),
    )
    view = Transcript([user_message("hi"), assistant, result]).for_request()
    assert len(view) == 3
    assert view[1].tool_uses()[0].id == "t1"
    assert view[2].tool_results()[0].tool_use_id == "t1"


def test_consecutive_user_messages_merge() -> None:
    view = Transcript([user_message("one"), user_message("two")]).for_request()
    assert len(view) == 1
    assert "one" in view[0].text() and "two" in view[0].text()


def test_consecutive_assistant_messages_get_neutral_user() -> None:
    first = assistant_message([TextBlock("a")])
    second = assistant_message([TextBlock("b")])
    view = Transcript([user_message("hi"), first, second]).for_request()
    assert [message.role for message in view] == ["user", "assistant", "user", "assistant"]


def test_starts_with_user() -> None:
    view = Transcript([assistant_message([TextBlock("hello")])]).for_request()
    assert view[0].role == "user"
