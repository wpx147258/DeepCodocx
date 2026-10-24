from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent.context.pipeline import SUMMARY_MAX_TOKENS, ContextPipeline
from cli_agent.context.spill import SpillStore
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
    root: Path,
    *,
    checkpoint=None,
    summarize=_summarize,
) -> ContextPipeline:
    paths = _paths(root)
    return ContextPipeline(
        estimator=TokenEstimator(),
        spill=SpillStore(paths.spills),
        session_paths=paths,
        summarize=summarize,
        checkpoint=checkpoint,
    )


def _tool_pair(tool_id: str, text: str) -> list:
    return [
        assistant_message([ToolUseBlock(tool_id, "Echo", {"text": "x"})]),
        tool_result_message([ToolResultBlock(tool_id, (TextBlock(text),))]),
    ]


def _tool_use_ids(messages) -> set[str]:
    ids: set[str] = set()
    for message in messages:
        for block in message.tool_uses():
            ids.add(block.id)
    return ids


async def test_large_latest_result_is_spilled(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path / "session")
    messages = [
        user_message("do the thing"),
        *_tool_pair("t1", "x" * 40_000),
    ]
    prepared = await pipeline.prepare(
        Transcript(messages), "do the thing", _profile(20_000)
    )
    result_texts = [
        block.content[0].text
        for message in prepared.messages
        for block in message.tool_results()
    ]
    assert any(text.startswith("[tool output spilled:") for text in result_texts)
    spill_files = list((tmp_path / "session" / "spills").glob("*.txt"))
    assert spill_files
    assert any(event.kind == "spill" for event in prepared.events)


async def test_old_results_are_pruned(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path / "session")
    messages = [user_message("start")]
    for index in range(6):
        messages.extend(_tool_pair(f"t{index}", "y" * 20_000))
        messages.append(assistant_message([TextBlock(f"step {index}")]))
    prepared = await pipeline.prepare(Transcript(messages), "start", _profile(20_000))
    spill_files = list((tmp_path / "session" / "spills").glob("*.txt"))
    assert len(spill_files) >= 2
    assert any(event.kind in ("spill", "prune") for event in prepared.events)


async def test_snip_archives_and_preserves_tool_pairs(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path / "session")
    messages = [user_message("start")]
    for index in range(8):
        messages.append(assistant_message([TextBlock("z" * 6_000)]))
        messages.extend(_tool_pair(f"t{index}", "small result"))
    prepared = await pipeline.prepare(Transcript(messages), "start", _profile(20_000))
    archives = list((tmp_path / "session" / "archive").glob("*.jsonl"))
    assert archives
    assert any(event.kind == "snip" for event in prepared.events)
    assert any("Earlier conversation archived" in message.text() for message in prepared.messages)
    ids = _tool_use_ids(prepared.messages)
    for message in prepared.messages:
        for result in message.tool_results():
            assert result.tool_use_id in ids


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


def _long_history() -> list:
    return [
        user_message("start"),
        *[assistant_message([TextBlock("z" * 6_000)]) for _ in range(8)],
    ]


def _recording_summarize(calls: list[int]):
    async def summarize(text: str, max_tokens: int) -> str:
        calls.append(max_tokens)
        return "SUMMARY"

    return summarize


@pytest.mark.parametrize("max_output", [0, 63])
async def test_summary_skips_when_output_limit_is_too_small(
    tmp_path: Path, max_output: int
) -> None:
    calls: list[int] = []
    pipeline = _pipeline(
        tmp_path / "session", summarize=_recording_summarize(calls)
    )
    prepared = await pipeline.prepare(
        Transcript(_long_history()),
        "start",
        _profile(20_000, max_output=max_output),
        force_compact=True,
    )
    skipped = [
        event
        for event in prepared.events
        if event.kind == "compact_skipped" and "output limit" in event.detail
    ]
    assert len(skipped) == 1
    assert calls == []


@pytest.mark.parametrize(
    ("max_output", "expected"),
    [(64, 64), (512, 512), (16_384, SUMMARY_MAX_TOKENS)],
)
async def test_summary_request_is_capped_to_profile(
    tmp_path: Path, max_output: int, expected: int
) -> None:
    calls: list[int] = []
    pipeline = _pipeline(
        tmp_path / "session", summarize=_recording_summarize(calls)
    )
    prepared = await pipeline.prepare(
        Transcript(_long_history()),
        "start",
        _profile(200_000, max_output=max_output),
        force_compact=True,
    )
    assert calls == [expected]
    assert any(event.kind == "compact" for event in prepared.events)


async def test_reactive_skip_is_reported_once(tmp_path: Path) -> None:
    calls: list[int] = []
    pipeline = _pipeline(
        tmp_path / "session", summarize=_recording_summarize(calls)
    )
    prepared = await pipeline.prepare(
        Transcript(_long_history()),
        "start",
        _profile(20_000, max_output=32),
        force_compact=True,
        reactive=True,
    )
    assert sum(event.kind == "compact_skipped" for event in prepared.events) == 1
    assert calls == []
