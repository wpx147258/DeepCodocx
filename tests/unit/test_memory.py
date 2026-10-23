from __future__ import annotations

import asyncio
from pathlib import Path

from cli_agent.context.memory import (
    ExtractionCoordinator,
    MemoryExtractor,
    MemoryRecord,
    MemoryStore,
)
from cli_agent.messages import user_message


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


async def test_extractor_saves_valid_json(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")

    async def complete(prompt: str) -> str:
        assert "Conversation" in prompt
        return (
            "Here is the result:\n"
            '[{"name": "test-rule", "description": "run tests", '
            '"type": "project", "content": "Always run tests before claiming done."}]'
        )

    extractor = MemoryExtractor(store, complete)
    records = await extractor.extract((user_message("please run tests"),))
    assert [record.name for record in records] == ["test-rule"]
    assert store.all()[0].name == "test-rule"


async def test_extractor_drops_secrets_and_duplicates(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    store.save(MemoryRecord("existing", "already here", "project", "keep"))

    async def complete(prompt: str) -> str:
        return (
            '[{"name": "existing", "description": "dup", "type": "project", "content": "dup"},'
            ' {"name": "secret", "description": "bad", "type": "project", '
            '"content": "api_key=sk-abcdefghijklmnop"},'
            ' {"name": "good", "description": "ok", "type": "feedback", "content": "Be brief."}]'
        )

    extractor = MemoryExtractor(store, complete)
    records = await extractor.extract((user_message("hi"),))
    assert [record.name for record in records] == ["good"]


async def test_coordinator_coalesces_and_reruns(tmp_path: Path) -> None:
    calls: list[int] = []

    class SlowExtractor:
        async def extract(self, messages):
            calls.append(1)
            await asyncio.sleep(0.05)
            return []

    messages = [user_message(f"m{index}") for index in range(5)]
    coordinator = ExtractionCoordinator(min_new_messages=1)
    first = asyncio.create_task(
        coordinator.request(lambda: tuple(messages), SlowExtractor())  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.01)
    messages.append(user_message("new message"))
    second = await coordinator.request(lambda: tuple(messages), SlowExtractor())  # type: ignore[arg-type]
    await first
    assert second == []
    assert len(calls) >= 2


async def test_coordinator_honors_cooldown() -> None:
    calls: list[int] = []

    class Extractor:
        async def extract(self, messages):
            calls.append(1)
            return []

    now = [0.0]
    coordinator = ExtractionCoordinator(
        min_new_messages=1, cooldown_seconds=60, clock=lambda: now[0]
    )
    messages = [user_message(f"m{index}") for index in range(4)]
    await coordinator.request(lambda: tuple(messages), Extractor())  # type: ignore[arg-type]
    assert len(calls) == 1

    messages.append(user_message("new"))
    now[0] = 30
    await coordinator.request(lambda: tuple(messages), Extractor())  # type: ignore[arg-type]
    assert len(calls) == 1  # still cooling down

    now[0] = 61
    await coordinator.request(lambda: tuple(messages), Extractor())  # type: ignore[arg-type]
    assert len(calls) == 2


async def test_extraction_failure_logs_warning_without_traceback(caplog) -> None:
    import logging

    class FailingExtractor:
        async def extract(self, messages):
            raise RuntimeError("boom")

    coordinator = ExtractionCoordinator(min_new_messages=1, cooldown_seconds=0)
    with caplog.at_level(logging.WARNING):
        await coordinator.request(
            lambda: (user_message("hi"),), FailingExtractor()  # type: ignore[arg-type]
        )
    assert "memory extraction skipped" in caplog.text
    assert "Traceback" not in caplog.text
