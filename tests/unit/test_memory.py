from __future__ import annotations

from pathlib import Path

from cli_agent.context.memory import (
    MemoryRecord,
    MemoryStore,
    looks_like_secret,
)
from cli_agent.tools.base import ToolContext
from cli_agent.tools.builtin.memory import RememberTool


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(cwd=tmp_path, session_id="test", data_dir=tmp_path)


def test_store_round_trip_and_index(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    store.save(
        MemoryRecord(
            name="concise",
            description="Prefers concise answers",
            type="user",
            content="Use short answers and skip preamble.",
        )
    )
    records = store.all()
    assert len(records) == 1
    assert records[0].name == "concise"
    assert records[0].type == "user"
    assert store.index_path.exists()
    assert "concise" in store.index_path.read_text(encoding="utf-8")

    selected = store.select("concise answers", limit=5)
    assert selected and selected[0].name == "concise"


def test_select_returns_nothing_for_unrelated_query(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    store.save(MemoryRecord("alpha", "alpha thing", "project", "alpha"))
    assert store.select("completely unrelated") == []


def test_secret_detection() -> None:
    assert looks_like_secret("api_key=sk-abcdefghijklmnop")
    assert looks_like_secret("password: hunter2")
    assert not looks_like_secret("The user prefers tabs over spaces.")


async def test_remember_tool_writes_a_record(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    tool = RememberTool(store)
    result = await tool.run(
        {
            "name": "prefers-tabs",
            "description": "Indentation preference",
            "type": "user",
            "content": "Use tabs in Python files; the project enforces it.",
        },
        _ctx(tmp_path),
    )
    assert not result.is_error
    assert "Saved memory" in result.text_content()
    records = store.all()
    assert [record.name for record in records] == ["prefers-tabs"]
    assert records[0].type == "user"


async def test_remember_updates_by_name(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    tool = RememberTool(store)
    args = {
        "name": "prefers-tabs",
        "description": "Indentation preference",
        "type": "user",
        "content": "Use tabs.",
    }
    await tool.run(args, _ctx(tmp_path))
    result = await tool.run({**args, "content": "Use tabs, never spaces."}, _ctx(tmp_path))
    assert "Updated memory" in result.text_content()
    assert len(store.all()) == 1
    assert store.all()[0].content == "Use tabs, never spaces."


async def test_remember_rejects_secrets_and_empty_content(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    tool = RememberTool(store)
    secret = await tool.run(
        {
            "name": "creds",
            "type": "reference",
            "content": "api_key=sk-abcdefghijklmnop",
        },
        _ctx(tmp_path),
    )
    assert secret.is_error
    empty = await tool.run(
        {"name": "blank", "type": "project", "content": "   "},
        _ctx(tmp_path),
    )
    assert empty.is_error
    assert store.all() == []


async def test_remember_rejects_unknown_type(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    tool = RememberTool(store)
    result = await tool.run(
        {"name": "x", "type": "banana", "content": "whatever"},
        _ctx(tmp_path),
    )
    assert result.is_error
    assert "unknown memory type" in result.text_content()


def test_no_background_extraction_surface_remains() -> None:
    import cli_agent.context.memory as memory_module

    for removed in (
        "MemoryExtractor",
        "ExtractionCoordinator",
        "ExtractionRunner",
    ):
        assert not hasattr(memory_module, removed)


async def test_remember_runs_through_the_agent_loop(tmp_path: Path) -> None:
    from tests.support.fake_provider import FakeProvider, text_response, tool_response
    from tests.support.runtime_fixture import make_runtime

    store = MemoryStore(tmp_path / "memory")
    provider = FakeProvider(
        [
            [
                tool_response(
                    [
                        (
                            "t1",
                            "Remember",
                            {
                                "name": "prefers-tabs",
                                "description": "Indentation preference",
                                "type": "user",
                                "content": "Use tabs in Python files.",
                            },
                        )
                    ]
                )
            ],
            [text_response("saved")],
        ]
    )
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=[RememberTool(store)])
    async for _ in runtime.run_turn("please remember that I use tabs"):
        pass

    assert [record.name for record in store.all()] == ["prefers-tabs"]
    result = runtime.messages[2].tool_results()[0]
    assert not result.is_error
    assert "Saved memory" in result.content[0].text
    assert "Remember" in [tool.name for tool in provider.requests[0].tools]
    await runtime.close()
