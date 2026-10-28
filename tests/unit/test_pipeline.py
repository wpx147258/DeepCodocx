from __future__ import annotations

from pathlib import Path

from cli_agent.context.pipeline import ContextPipeline
from cli_agent.context.tokens import TokenEstimator
from cli_agent.messages import (
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    assistant_message,
    tool_result_message,
    user_message,
)
from cli_agent.providers.base import ModelProfile
from cli_agent.session.paths import SessionPaths
from cli_agent.transcript import Transcript


async def _summarize(text: str, max_tokens: int) -> str:
    return "SUMMARY: " + text[:80]


def _paths(root: Path) -> SessionPaths:
    return SessionPaths(
        root=root,
        jsonl=root / "session.jsonl",
        context=root / "context.json",
        meta=root / "meta.json",
        lock=root / "lock",
        spills=root / "spills",
        archive=root / "archive",
    )


def _profile(context_window: int, max_output: int = 1_000) -> ModelProfile:
    return ModelProfile(
        provider_name="mock",
        provider_kind="anthropic_compat",
        model="mock",
        context_window=context_window,
        max_output_tokens=max_output,
    )


def _pipeline(
    root: Path, *, checkpoint=None, summarize=_summarize
) -> ContextPipeline:
    paths = _paths(root)
    return ContextPipeline(
        estimator=TokenEstimator(),
        session_paths=paths,
        summarize=summarize,
        checkpoint=checkpoint,
    )


def _tool_pair(tool_id: str, text: str) -> list:
    return [
        assistant_message([ToolUseBlock(tool_id, "Echo", {"text": "x"})]),
        tool_result_message([ToolResultBlock(tool_id, (TextBlock(text),))]),
    ]


async def test_force_compact_creates_checkpoint(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path / "session")
    messages = [user_message("the request")]
    for index in range(12):
        messages.append(assistant_message([TextBlock(f"answer {index} " + "q" * 1_000)]))
        messages.append(user_message(f"follow up {index} " + "r" * 1_000))
    prepared = await pipeline.prepare(
        Transcript(messages), "the request", _profile(10_000), force_compact=True
    )
    assert prepared.checkpoint is not None
    assert prepared.messages[0].text().startswith("[Compacted context]")
    assert "the request" in prepared.messages[0].text()
    assert any(event.kind == "compact" for event in prepared.events)


async def test_checkpoint_is_reapplied_on_resume(tmp_path: Path) -> None:
    root = tmp_path / "session"
    first = _pipeline(root)
    messages = [user_message("the request")]
    for index in range(12):
        messages.append(assistant_message([TextBlock(f"answer {index} " + "q" * 1_000)]))
        messages.append(user_message(f"follow up {index} " + "r" * 1_000))
    prepared = await first.prepare(
        Transcript(messages), "the request", _profile(10_000), force_compact=True
    )
    assert prepared.checkpoint is not None

    second = _pipeline(root, checkpoint=prepared.checkpoint)
    resumed = await second.prepare(
        Transcript(messages), "the request", _profile(10_000)
    )
    assert resumed.messages[0].text().startswith("[Compacted context]")
    assert "the request" in resumed.messages[0].text()


async def test_short_history_does_not_compact(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path / "session")
    prepared = await pipeline.prepare(
        Transcript([user_message("hi"), assistant_message([TextBlock("hello")])]),
        "hi",
        _profile(10_000),
        force_compact=True,
    )
    assert prepared.checkpoint is None
    assert any(event.kind == "compact_skipped" for event in prepared.events)


async def test_force_compact_works_when_whole_view_fits_window(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path / "session")
    messages = [user_message("the request")]
    for index in range(5):
        messages.append(assistant_message([TextBlock(f"answer {index}")]))
        messages.append(user_message(f"question {index}"))
    prepared = await pipeline.prepare(
        Transcript(messages), "the request", _profile(200_000), force_compact=True
    )
    assert prepared.checkpoint is not None
    assert prepared.messages[0].text().startswith("[Compacted context]")


async def test_history_is_never_rewritten(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path / "session")
    messages = [user_message("start")]
    for index in range(6):
        messages.extend(_tool_pair(f"t{index}", "y" * 20_000))
        messages.append(assistant_message([TextBlock(f"step {index}")]))

    prepared = await pipeline.prepare(Transcript(messages), "start", _profile(60_000))

    expected = Transcript(messages).for_request()
    assert [(m.role, m.text()) for m in prepared.messages] == [
        (m.role, m.text()) for m in expected
    ]
    assert not list((tmp_path / "session" / "spills").glob("*.txt"))
    assert not list((tmp_path / "session" / "archive").glob("*.jsonl"))
    assert not prepared.events


async def test_summary_skips_when_the_input_budget_is_too_small(
    tmp_path: Path,
) -> None:
    calls: list[int] = []

    async def summarize(text: str, max_tokens: int) -> str:
        calls.append(max_tokens)
        return "SUMMARY"

    pipeline = _pipeline(tmp_path / "session", summarize=summarize)
    messages = [user_message("start")]
    for index in range(6):
        messages.append(assistant_message([TextBlock(f"step {index} " + "z" * 6_000)]))
    prepared = await pipeline.prepare(
        Transcript(messages),
        "start",
        _profile(8_000, max_output=2_000),
        force_compact=True,
    )
    assert any(
        event.kind == "compact_skipped" and "summary input" in event.detail
        for event in prepared.events
    )
    assert calls == []


async def test_summary_input_is_bounded_by_the_profile(tmp_path: Path) -> None:
    texts: list[str] = []

    async def summarize(text: str, max_tokens: int) -> str:
        texts.append(text)
        return "SUMMARY"

    pipeline = _pipeline(tmp_path / "session", summarize=summarize)
    messages = [user_message("start")]
    for index in range(6):
        messages.append(assistant_message([TextBlock(f"step {index} " + "z" * 20_000)]))
    prepared = await pipeline.prepare(
        Transcript(messages),
        "start",
        _profile(20_000, max_output=2_000),
        force_compact=True,
    )
    assert texts, "summary should still run"
    assert "summary input truncated" in texts[0]
    input_budget = 20_000 - 2_000 - 4_096 - 2_000 - 1_024
    assert TokenEstimator().estimate_text(texts[0]) <= input_budget
    assert any(event.kind == "compact" for event in prepared.events)
