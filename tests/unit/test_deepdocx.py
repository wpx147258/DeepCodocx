from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from dcx import assembly
from dcx.assembly import BuildOptions, build_runtime
from dcx.config import load_config
from dcx.deepdocx.library import DocumentLibrary, LibraryStore
from dcx.deepdocx.profile import SessionProfile
from dcx.deepdocx.server import DocumentServer
from dcx.mcp.client import ServerConfig, StdioClient, normalize_result
from dcx.mcp.tools import CallExternalTool, DiscoverTools, ExternalCatalog
from dcx.messages import (
    ImageBlock,
    ToolUseBlock,
    message_from_dict,
    message_to_dict,
    user_message,
)
from dcx.permissions.policy import PermissionPolicy
from dcx.providers.base import ModelProfile
from dcx.session.paths import latest_session_id, list_session_ids
from dcx.session.store import SessionStore
from dcx.tools.execution import ToolExecutor
from dcx.tools.registry import ToolRegistry
from dcx.web.server import create_app
from tests.support.document_fixture import PNG, document_library
from tests.support.fake_provider import FakeProvider, text_response, tool_response
from tests.support.runtime_fixture import make_runtime


def test_document_protocol_validation_and_guide(tmp_path: Path):
    server = DocumentServer(DocumentLibrary(document_library(tmp_path / "bundles")))
    assert server.handle({"method": "notifications/initialized"}) is None
    assert "tools" in server.handle({"id": 1, "method": "tools/list"})["result"]
    assert "error" in server.handle({"id": 2, "method": "missing"})
    for name, arguments in [
        ("doc_library", {}),
        ("doc_guide", {}),
        ("doc_search", {"query": "memory"}),
        ("doc_read", {"locator": "doc:paper_alpha@v1/basic/page:1"}),
        ("doc_view", {"locator": "doc:paper_alpha@v1/marker-json-v1/unit:u_figure"}),
    ]:
        assert not server.call(name, arguments)["isError"]
    invalid = server.handle(
        {"id": 3, "method": "tools/call", "params": {"name": "doc_read", "arguments": {}}}
    )
    assert invalid["result"]["isError"]


def test_readonly_fts_and_corrupt_index_fallback(tmp_path: Path):
    root = document_library(tmp_path / "bundles")
    db = root / "catalog.sqlite"
    with contextlib.closing(sqlite3.connect(db)) as connection:
        connection.executescript(
            "CREATE TABLE units(unit_key TEXT,doc_id TEXT,version TEXT,chunk_id TEXT,kind TEXT,text TEXT,page_start INTEGER,bbox_json TEXT); CREATE VIRTUAL TABLE units_fts USING fts5(unit_key UNINDEXED,text);"
        )
        connection.execute(
            "INSERT INTO units VALUES(?,?,?,?,?,?,?,?)",
            (
                "key",
                "paper_alpha",
                "v1",
                "c_0",
                "text",
                "Memory stores visual tokens.",
                1,
                "[0,0,100,20]",
            ),
        )
        connection.execute(
            "INSERT INTO units_fts VALUES(?,?)", ("key", "Memory stores visual tokens.")
        )
        connection.commit()
    original = db.read_bytes()
    result = DocumentLibrary(root).search("memory", doc="paper_alpha")
    assert any(hit["rendition"] == "basic" for hit in result["hits"])
    assert db.read_bytes() == original
    db.write_bytes(b"broken-index")
    result = DocumentLibrary(root).search("memory")
    assert result["hits"] and result["warnings"]
    assert db.read_bytes() == b"broken-index"


@pytest.mark.parametrize("behavior", ["normal", "hang", "malformed"])
async def test_mcp_notifications_pagination_timeout_and_cleanup(behavior):
    config = ServerConfig(
        "fixture", (sys.executable, "-m", "tests.support.mcp_fixture_server", behavior), timeout=0.5
    )
    client = StdioClient(config)
    try:
        assert [tool["name"] for tool in await client.tools()] == ["first", "second"]
        process = client._process
        if behavior == "normal":
            assert (
                normalize_result(await client.request("tools/call", {})).text_content()
                == "fixture-ok"
            )
        else:
            with pytest.raises((TimeoutError, ValueError)):
                await client.request("tools/call", {})
            assert process.returncode is not None
    finally:
        await client.close()


async def test_mcp_cancellation_closes_peer():
    client = StdioClient(
        ServerConfig("fixture", (sys.executable, "-m", "tests.support.mcp_fixture_server", "hang"))
    )
    await client.tools()
    process = client._process
    task = asyncio.create_task(client.request("tools/call", {}))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None
    await client.close()


def server_config(root: Path, *, read: bool = True) -> ServerConfig:
    return ServerConfig(
        "documents",
        (sys.executable, "-m", "dcx.deepdocx.server", "--root", str(root)),
        read_tools=("doc_search", "doc_view", "doc_read", "doc_library", "doc_guide")
        if read
        else (),
    )


def test_heterogeneous_locations_read_images_and_scope(tmp_path: Path):
    root = document_library(tmp_path / "bundles")
    library = DocumentLibrary(root)
    assert len(library.documents()) == 1
    assert len(library.documents()[0]["renditions"]) == 2
    hits = library.search("memory")["hits"]
    assert {hit["rendition"] for hit in hits} == {"basic", "marker-json-v1"}
    for hit in hits:
        read = library.read(hit["locator"], neighbors=1)
        assert read["text"]
        assert read["evidence"][0]["rendition"] == hit["rendition"]
    image, mime, evidence = library.image("doc:paper_alpha@v1/marker-json-v1/unit:u_figure")
    assert image.read_bytes() == PNG and mime == "image/png" and evidence["page"] == 1
    focused = library.read(
        "doc:paper_alpha@v1/marker-json-v1/unit:u_figure", max_chars=50, neighbors=20
    )
    assert "Figure 1: memory architecture" in focused["text"]
    assert focused["source"]["locator"].endswith("unit:u_figure")
    assert DocumentLibrary(root, ("unknown",)).documents() == []
    with pytest.raises(ValueError, match="rendition"):
        library.read("doc:paper_alpha@v1/basic/unit:u_paragraph")
    with pytest.raises(ValueError):
        library.read("doc:paper_alpha@v2/basic/unit:c_0")


def test_asset_escape_and_incomplete_bundles(tmp_path: Path):
    root = document_library(tmp_path / "bundles")
    asset = root / "paper_alpha/v1/basic/assets/page.png"
    asset.unlink()
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    asset.symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        DocumentLibrary(root).image("doc:paper_alpha@v1/basic/page:1")
    (root / "paper_alpha/v1/marker/units.jsonl").unlink()
    assert len(DocumentLibrary(root).documents()[0]["renditions"]) == 1


def test_corrupt_units_do_not_block_other_renditions(tmp_path: Path):
    root = document_library(tmp_path / "bundles")
    (root / "paper_alpha/v1/marker/units.jsonl").write_text("invalid-json")
    result = DocumentLibrary(root).search("memory")
    assert result["hits"] and result["warnings"]
    assert all(hit["rendition"] == "basic" for hit in result["hits"])


def test_registry_persists_normalized_data_root(tmp_path: Path):
    root = document_library(tmp_path / "corpus/paper-bundles")
    store = LibraryStore(tmp_path / "state")
    entry = store.register(str(root.parent), "Papers")
    assert store.get(entry["id"])["root"] == str(root)
    assert LibraryStore(tmp_path / "state").entries() == [entry]
    assert not (root / "libraries.json").exists()


def test_library_default_and_last_selection_survive_restart_and_registration(tmp_path: Path):
    first = document_library(tmp_path / "first")
    second = document_library(tmp_path / "second")
    third = document_library(tmp_path / "third")
    store = LibraryStore(tmp_path / "state", (str(first),), default_root=str(second))
    default = store.get(store.selected_id())
    assert default["root"] == str(second)
    chosen = next(entry for entry in store.entries() if entry["root"] == str(first))
    store.select(chosen["id"])
    store.register(str(third))
    restarted = LibraryStore(tmp_path / "state", default_root=str(second))
    assert restarted.selected_id() == chosen["id"]
    assert len(restarted.entries()) == 3
    assert json.loads(restarted.path.read_text())["selected_library_id"] == chosen["id"]


def test_library_fallback_discovers_complete_corpus_and_remembers_it(tmp_path: Path):
    root = document_library(tmp_path / "deep-docs-unified/data/paper-bundles")
    store = LibraryStore(
        tmp_path / "state", fallback_roots=(str(tmp_path / "missing"), str(root.parent))
    )
    selected = store.get(store.selected_id())
    assert selected["root"] == str(root)
    assert not store.path.exists()
    store.select(selected["id"])
    assert LibraryStore(tmp_path / "state").get(selected["id"]) == selected


def test_missing_selected_library_falls_back_to_available_default(tmp_path: Path):
    first = document_library(tmp_path / "first")
    second = document_library(tmp_path / "second")
    store = LibraryStore(tmp_path / "state", default_root=str(second))
    saved = store.register(str(first))
    store.select(saved["id"])
    first.rename(tmp_path / "moved")
    restarted = LibraryStore(tmp_path / "state", default_root=str(second))
    assert restarted.get(restarted.selected_id())["root"] == str(second)


async def test_real_stdio_discovery_read_and_image(tmp_path: Path):
    root = document_library(tmp_path / "bundles")
    client = StdioClient(server_config(root))
    try:
        names = {tool["name"] for tool in await client.tools()}
        assert names == {"doc_search", "doc_read", "doc_view", "doc_library", "doc_guide"}
        result = await client.request(
            "tools/call", {"name": "doc_search", "arguments": {"query": "memory"}}
        )
        hits = json.loads(normalize_result(result).text_content())["hits"]
        assert hits
        result = normalize_result(
            await client.request(
                "tools/call", {"name": "doc_view", "arguments": {"locator": hits[0]["locator"]}}
            )
        )
        assert any(isinstance(block, ImageBlock) for block in result.content)
        bad = normalize_result(
            await client.request(
                "tools/call", {"name": "doc_read", "arguments": {"locator": "missing"}}
            )
        )
        assert bad.is_error
        process = client._process
    finally:
        await client.close()
    assert process is not None and process.returncode is not None


async def test_external_actual_schema_and_permission_gate(tmp_path: Path):
    root = document_library(tmp_path / "bundles")
    catalog = ExternalCatalog([server_config(root, read=False)])
    registry = ToolRegistry()
    registry.register(DiscoverTools(catalog))
    registry.register(CallExternalTool())
    executor = ToolExecutor(registry, external=catalog)
    policy = PermissionPolicy("default", workspace=tmp_path)
    try:
        denied = await executor.prepare(
            ToolUseBlock(
                "c1",
                "CallExternalTool",
                {"tool": "documents.doc_search", "arguments": {"query": "memory"}},
            ),
            policy=policy,
            asker=None,
        )
        assert denied.early_error and "non-interactive" in denied.early_error.text_content()
        invalid = await executor.prepare(
            ToolUseBlock(
                "c2",
                "CallExternalTool",
                {"tool": "documents.doc_search", "arguments": {"top_k": 900}},
            ),
            policy=policy,
            asker=None,
        )
        assert invalid.early_error and "query" in invalid.early_error.text_content()
        assert {spec.name for spec in registry.specs()} == {"DiscoverTools", "CallExternalTool"}
    finally:
        await catalog.close()


async def test_one_runtime_can_discover_search_and_view(tmp_path: Path):
    root = document_library(tmp_path / "bundles")
    catalog = ExternalCatalog([server_config(root)])
    provider = FakeProvider(
        [
            [tool_response([("t1", "DiscoverTools", {"query": "paper search image"})])],
            [
                tool_response(
                    [
                        (
                            "t2",
                            "CallExternalTool",
                            {"tool": "documents.doc_search", "arguments": {"query": "memory"}},
                        )
                    ]
                )
            ],
            [
                tool_response(
                    [
                        (
                            "t3",
                            "CallExternalTool",
                            {
                                "tool": "documents.doc_view",
                                "arguments": {
                                    "locator": "doc:paper_alpha@v1/marker-json-v1/unit:u_figure"
                                },
                            },
                        )
                    ]
                )
            ],
            [
                text_response(
                    "Explanation [source](doc:paper_alpha@v1/marker-json-v1/unit:u_figure)"
                )
            ],
        ]
    )
    runtime, _ = make_runtime(
        provider, tmp_path=tmp_path, tools=[DiscoverTools(catalog), CallExternalTool()]
    )
    runtime._deps.executor.external = catalog
    runtime._deps.external = catalog
    runtime._deps.session_profile = SessionProfile("deepdocx", "lib_test", str(root))
    try:
        events = [
            event async for event in runtime.run_turn("Explain this paper's memory architecture")
        ]
        assert events[-1].stop_reason == "end_turn"
        assert len(provider.requests) == 4
        assert all(
            request.system == provider.requests[0].system
            and request.tools == provider.requests[0].tools
            for request in provider.requests
        )
        assert any(
            isinstance(block, ImageBlock)
            for result in runtime.messages[-2].tool_results()
            for block in result.content
        )
    finally:
        await runtime.close()


async def test_profile_keeps_system_and_prior_wire_prefix_stable(tmp_path: Path):
    provider = FakeProvider([[text_response("first")], [text_response("second")]])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    baseline, _ = runtime._build_request([])
    runtime._deps.session_profile = SessionProfile("deepdocx", "lib_test", "/test/library")
    try:
        _ = [event async for event in runtime.run_turn("Explain memory")]
        first = provider.requests[0]
        _ = [event async for event in runtime.run_turn("Explain the next step")]
        second = provider.requests[1]
        assert first.system == second.system == baseline.system
        assert first.tools == baseline.tools
        assert second.messages[0] == first.messages[0]
        assert "Host session context" in first.messages[0].text()
        assert runtime.messages[0].text() == "Explain memory"
        assert message_from_dict(message_to_dict(runtime.messages[0])) == runtime.messages[0]
        await runtime.clear()
        assert runtime.session_profile.mode == "deepdocx"
    finally:
        await runtime.close()


def setup_build(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        assembly,
        "build_provider",
        lambda *args, **kwargs: (
            FakeProvider([[text_response("ok")]]),
            ModelProfile("anthropic", "anthropic_compat", "fake-model", 200_000, 8192),
        ),
    )
    return BuildOptions(
        cwd=tmp_path,
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "state")},
        memory_enabled=False,
    )


def test_late_assembly_failure_releases_provider_and_session_lock(tmp_path: Path, monkeypatch):
    options = setup_build(tmp_path, monkeypatch)
    provider = FakeProvider([])
    monkeypatch.setattr(
        assembly,
        "build_provider",
        lambda *args, **kwargs: (
            provider,
            ModelProfile("anthropic", "anthropic_compat", "test", 200000, 8192),
        ),
    )

    def fail(*args):
        raise RuntimeError("failed to load instructions")

    monkeypatch.setattr(assembly, "load_project_instructions", fail)
    with pytest.raises(RuntimeError, match="instructions"):
        build_runtime(options)
    assert provider.closed
    paths = load_config(cwd=tmp_path, env=options.env).paths
    saved = list_session_ids(paths)
    assert len(saved) == 1
    store, _ = SessionStore.resume(paths, saved[0])
    store.close()


async def test_assembly_system_and_tools_identical_across_modes_and_discovery(
    tmp_path: Path, monkeypatch
):
    options = setup_build(tmp_path, monkeypatch)
    root = document_library(tmp_path / "bundles")
    normal = build_runtime(options)
    deep = build_runtime(replace(options, library=str(root)))
    try:
        messages = [user_message("Explain memory")]
        ordinary, _ = normal.runtime._build_request(messages)
        before, _ = deep.runtime._build_request(messages)
        await deep.runtime._deps.external.discover("paper read view", 5)
        after, _ = deep.runtime._build_request(messages)
        assert ordinary.system == before.system == after.system
        assert ordinary.tools == before.tools == after.tools
        assert "DeepDocx" not in "\n".join(section.text for section in ordinary.system)
        assert "Host session context" in before.messages[0].text()
        assert "Host session context" not in ordinary.messages[0].text()
    finally:
        await normal.runtime.close()
        await deep.runtime.close()


async def test_compaction_rehydrates_message_guidance_without_mutating_history(tmp_path: Path):
    provider = FakeProvider([[text_response("answer " * 40)] for _ in range(6)])
    runtime, _ = make_runtime(provider, tmp_path=tmp_path)
    runtime._deps.session_profile = SessionProfile("deepdocx", "lib_test", "/test/library")
    try:
        for index in range(6):
            _ = [event async for event in runtime.run_turn(f"question {index} " * 30)]
        original = runtime.messages
        _ = [event async for event in runtime.compact()]
        prepared = await runtime._deps.context.prepare(
            runtime._deps.transcript, "question", runtime._deps.profile
        )
        request, _ = runtime._build_request(prepared.messages)
        assert "Host session context" in "\n".join(message.text() for message in request.messages)
        assert runtime.messages == original
    finally:
        await runtime.close()


async def test_persistent_fork_resume_mode_history_and_boundary(tmp_path: Path, monkeypatch):
    options = setup_build(tmp_path, monkeypatch)
    original = build_runtime(options)
    root = document_library(tmp_path / "bundles")
    try:
        _ = [event async for event in original.runtime.run_turn("Original conversation")]
        original_bytes = original.runtime._deps.session.jsonl_path.read_bytes()
        child = build_runtime(
            replace(options, resume_id=original.session_id, fork=True, library=str(root))
        )
        child_id = child.session_id
        try:
            assert child.runtime.session_profile.forked_from == original.session_id
            assert child.runtime.session_profile.fork_boundary == 2
            assert child.runtime.messages == original.runtime.messages
            with pytest.raises(ValueError, match="fork boundary"):
                await child.runtime.rewind_to(child.runtime.messages[0].id)
            assert original.runtime._deps.session.jsonl_path.read_bytes() == original_bytes
            assert latest_session_id(original.config.paths) == original.session_id
            assert latest_session_id(original.config.paths, mode="deepdocx") == child_id
            assert list_session_ids(original.config.paths, mode="normal") == [original.session_id]
        finally:
            await child.runtime.close()
        resumed = build_runtime(replace(options, resume_id=child_id))
        try:
            assert resumed.runtime.session_profile.mode == "deepdocx"
            assert resumed.runtime.session_profile.library_root == str(root)
        finally:
            await resumed.runtime.close()
    finally:
        await original.runtime.close()


async def test_web_register_fork_sources_and_history(tmp_path: Path, monkeypatch):
    options = setup_build(tmp_path, monkeypatch)
    bundle = build_runtime(options)
    _ = [event async for event in bundle.runtime.run_turn("Explain my work")]
    root = document_library(tmp_path / "corpus/paper-bundles")
    app = create_app(bundle, factory=lambda ident: build_runtime(replace(options, resume_id=ident)))
    host = app.state.host
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            library = (await client.post("/api/libraries", json={"root": str(root.parent)})).json()[
                "libraries"
            ][0]
            response = await client.post(
                "/api/mode", json={"mode": "deepdocx", "library_id": library["id"], "fork": True}
            )
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["mode"] == "deepdocx" and len(data["messages"]) == 2
            assert data["session_context"]["fork_boundary"] == 2
            sessions = (await client.get("/api/sessions")).json()["sessions"]
            assert [item["id"] for item in sessions] == [data["session_id"]]
            docs = (await client.get("/api/documents")).json()["documents"]
            assert len(docs) == 1
            locator = "doc:paper_alpha@v1/marker-json-v1/unit:u_figure"
            evidence = await client.get("/api/evidence", params={"locator": locator})
            assert evidence.status_code == 200
            assert evidence.json()["image_locator"] == locator
            assert evidence.json()["source"]["locator"] == locator
            image = await client.get("/api/evidence", params={"locator": locator, "image": "1"})
            assert image.content == PNG
            normal = (await client.post("/api/mode", json={"mode": "normal"})).json()
            assert normal["session_id"] == bundle.session_id
            assert normal["mode"] == "normal"
    finally:
        await host.close()


async def test_default_config_discovers_nearby_library_without_manual_path(
    tmp_path: Path, monkeypatch
):
    options = setup_build(tmp_path, monkeypatch)
    workspace = tmp_path / "cli_agent"
    workspace.mkdir()
    corpus = document_library(tmp_path / "deep-docs-unified/data/paper-bundles")
    options = replace(options, cwd=workspace)
    normal = build_runtime(options)
    deep = build_runtime(replace(options, mode="deepdocx"))
    try:
        assert normal.libraries.get(normal.libraries.selected_id())["root"] == str(corpus)
        assert deep.runtime.session_profile.library_root == str(corpus)
        remembered = LibraryStore(normal.config.paths.data_dir)
        assert remembered.get(remembered.selected_id())["root"] == str(corpus)
    finally:
        await normal.runtime.close()
        await deep.runtime.close()


async def test_web_defaults_and_user_selection_persist_across_server_restart(
    tmp_path: Path, monkeypatch
):
    options = setup_build(tmp_path, monkeypatch)
    first = document_library(tmp_path / "first")
    second = document_library(tmp_path / "second")
    third = document_library(tmp_path / "third")
    config = tmp_path / "settings.toml"
    config.write_text("[deepdocx]\ndefault_library = " + json.dumps(str(first)) + "\n")
    options = replace(options, config_path=config)
    original = build_runtime(options)
    app = create_app(original)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            defaults = (await client.get("/api/libraries")).json()
            assert len(defaults["libraries"]) == 1
            assert defaults["selected_library_id"] == defaults["libraries"][0]["id"]
            added = (await client.post("/api/libraries", json={"root": str(second)})).json()
            chosen_id = added["registered_library_id"]
            response = await client.post(
                "/api/mode", json={"mode": "deepdocx", "library_id": chosen_id}
            )
            assert response.status_code == 200, response.text
            await client.post("/api/libraries", json={"root": str(third)})
            # Reconnecting an existing root identifies that root, not the last item.
            reconnected = (await client.post("/api/libraries", json={"root": str(first)})).json()
            assert reconnected["registered_library_id"] == defaults["selected_library_id"]
            assert reconnected["selected_library_id"] == chosen_id
    finally:
        await app.state.host.close()
    restarted = build_runtime(options)
    app = create_app(restarted)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            saved = (await client.get("/api/libraries")).json()
            assert saved["selected_library_id"] == chosen_id
            assert len(saved["libraries"]) == 3
            response = await client.post("/api/mode", json={"mode": "deepdocx"})
            assert response.status_code == 200, response.text
            assert response.json()["session_context"]["library_root"] == str(second)
    finally:
        await app.state.host.close()
