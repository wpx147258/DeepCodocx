from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from dataclasses import asdict, replace

import pytest
from PIL import Image

from dcx import assembly
from dcx.assembly import BuildOptions, build_runtime
from dcx.deepdocx.library import DocumentLibrary
from dcx.deepdocx.semantic.service import IndexService
from dcx.deepdocx.semantic.tools import call
from dcx.deepdocx.server import DocumentServer
from dcx.messages import ImageBlock, user_message
from dcx.providers.base import ModelProfile
from dcx.session.attachments import AttachmentStore
from tests.support.document_fixture import document_library
from tests.support.fake_provider import FakeProvider, text_response, tool_response
from tests.support.inference_fixture import inference_server
from tests.unit.test_semantic import engine
from tests.unit.test_semantic_service import configuration, wait


def image_block() -> ImageBlock:
    image = Image.new("RGB", (64, 64), "#346768")
    content = io.BytesIO()
    image.save(content, format="PNG")
    return ImageBlock("image/png", base64.b64encode(content.getvalue()).decode())


def test_attachment_refs_survive_copy_and_detect_tampering_and_other_sessions(tmp_path):
    import shutil

    source = AttachmentStore(tmp_path / "source")
    image = image_block()
    reference = source.save(image)
    assert source.resolve(reference).read_bytes() == base64.b64decode(image.data)
    with pytest.raises(ValueError, match="not available"):
        AttachmentStore(tmp_path / "another").resolve(reference)
    shutil.copytree(tmp_path / "source", tmp_path / "fork")
    child = AttachmentStore(tmp_path / "fork")
    assert child.resolve(reference).read_bytes() == source.resolve(reference).read_bytes()
    source.resolve(reference).write_bytes(b"changed")
    with pytest.raises(ValueError, match="does not match"):
        source.resolve(reference)
    assert child.resolve(reference).is_file()
    with pytest.raises(ValueError, match="Invalid"):
        source.resolve("attachment:../../outside")


def test_attachment_symlinks_are_refused_and_corrupt_images_keep_original_input(tmp_path):
    store = AttachmentStore(tmp_path / "session")
    reference = store.save(image_block())
    path = store.resolve(reference)
    original = tmp_path / "original.png"
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(ValueError, match="symlink"):
        store.resolve(reference)
    invalid = ImageBlock("image/png", base64.b64encode(b"invalid image").decode())
    message = user_message("inspect", images=[invalid])
    view = store.enrich(message)
    assert view.content == message.content and message.host_context == ""
    assert "reference unavailable" in view.host_context


def test_unified_tool_routes_targets_and_returns_bounded_source_cards(tmp_path):
    e = engine(tmp_path)
    e.sync()
    attachments = AttachmentStore(tmp_path / "session")
    reference = attachments.save(image_block())
    server = DocumentServer(e.library, e, (tmp_path,), attachments)
    text = json.loads(
        server.call("doc_semantic_search", {"query": "memory", "target": "text"})["content"][0][
            "text"
        ]
    )
    assert text["query_mode"] == "text"
    assert all(hit["matched_representations"] == ["text_block"] for hit in text["hits"])
    assert all(hit["snippet_source"] == "source_text" for hit in text["hits"])
    assert "image" not in text["hits"][0] and "text" not in text["hits"][0]
    mixed = call(
        e,
        "doc_semantic_search",
        {"query": "architecture", "image_ref": reference},
        (tmp_path,),
        attachments,
    )
    assert mixed["query_mode"] == "image_text" and mixed["hits"]
    assert all("text_block" not in hit["matched_representations"] for hit in mixed["hits"])
    assert mixed["coverage"]["indexed_sources"][0]["doc_id"] == "paper_alpha"
    result = server.call("doc_semantic_search", {"image_ref": reference})
    assert all(block["type"] == "text" for block in result["content"])
    source = json.loads(result["content"][0]["text"])["hits"][0]["locator"]
    assert any(
        block["type"] == "image"
        for block in server.call("doc_view", {"locator": source})["content"]
    )
    for args in (
        {"image_ref": reference, "image_path": str(tmp_path)},
        {"query": "x", "target": "image", "representations": ["image"]},
    ):
        with pytest.raises(ValueError):
            call(e, "doc_semantic_search", args, (tmp_path,), attachments)


def test_unindexed_and_out_of_scope_papers_have_explicit_coverage(tmp_path):
    import shutil

    e = engine(tmp_path)
    other = e.library.root / "paper_beta/v1/basic"
    shutil.copytree(e.library.root / "paper_alpha/v1/basic", other)
    manifest = other / "manifest.json"
    raw = json.loads(manifest.read_text())
    raw["doc"]["doc_id"] = "paper_beta"
    manifest.write_text(json.dumps(raw))
    e.sync(documents=["paper_alpha"])
    result = e.search("memory")
    assert result["coverage"]["total_documents"] == 2
    assert result["coverage"]["searchable_documents"] == 1
    assert [item["doc_id"] for item in result["coverage"]["indexed_sources"]] == ["paper_alpha"]
    assert result["warnings"]
    pending = e.search("memory", doc="paper_beta")
    assert not pending["hits"] and pending["coverage"]["requested_doc_status"] == "pending"
    outside = e.search("memory", doc="outside")
    assert outside["coverage"]["requested_doc_status"] == "not_in_scope"


async def test_assembly_attachment_context_is_stable_and_restores_in_a_fork(tmp_path, monkeypatch):
    options = BuildOptions(
        cwd=tmp_path,
        env={"XDG_DATA_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "config")},
        memory_enabled=False,
    )
    root = document_library(tmp_path / "corpus")
    providers = []

    def provider(*args, **kwargs):
        fake = FakeProvider([[text_response("first")], [text_response("second")]])
        providers.append(fake)
        return fake, ModelProfile(
            "anthropic", "anthropic_compat", "fake", 200000, 8192, supports_images=True
        )

    monkeypatch.setattr(assembly, "build_provider", provider)
    bundle = build_runtime(replace(options, library=str(root)))
    ordinary = build_runtime(options)
    source_id = bundle.session_id
    try:
        _ = [
            event
            async for event in bundle.runtime.run_turn("Find this image", images=[image_block()])
        ]
        first = providers[0].requests[0]
        reference = re.search(r"attachment:[0-9a-f]{64}", first.messages[0].text())[0]
        assert all("attachment:" not in message.host_context for message in bundle.runtime.messages)
        _ = [event async for event in bundle.runtime.run_turn("continue")]
        assert first.messages[0] == providers[0].requests[1].messages[0]
        normal, _ = ordinary.runtime._build_request([user_message("same")])
        assert normal.system == first.system and normal.tools == first.tools
        # A fresh compaction prefix must preserve attachment context when the
        # mode introduction is reattached by the runtime.
        fresh = AttachmentStore(bundle.runtime._deps.session.paths.root).enrich(
            user_message("after compaction", images=[image_block()])
        )
        request, _ = bundle.runtime._build_request([fresh])
        assert reference in request.messages[0].text()
        history = bundle.runtime._deps.session.paths.jsonl.read_bytes()
    finally:
        await bundle.runtime.close()
        await ordinary.runtime.close()
    child = build_runtime(replace(options, resume_id=source_id, fork=True))
    try:
        # The fork keeps reference IDs stable while resolving to the copied assets.
        assets = AttachmentStore(child.runtime._deps.session.paths.root)
        assert assets.resolve(reference).is_file()
        _ = [event async for event in child.runtime.run_turn("use the previous image")]
        assert reference in providers[-1].requests[0].messages[0].text()
        assert (
            bundle.config.paths.session_dir(source_id) / "session.jsonl"
        ).read_bytes() == history
    finally:
        await child.runtime.close()


async def test_bootstrap_discovery_includes_semantic_tool(tmp_path, monkeypatch):
    fake = FakeProvider([])
    monkeypatch.setattr(
        assembly,
        "build_provider",
        lambda *args, **kwargs: (
            fake,
            ModelProfile("anthropic", "anthropic_compat", "fake", 200000, 8192),
        ),
    )
    bundle = build_runtime(
        BuildOptions(
            cwd=tmp_path,
            library=str(document_library(tmp_path / "corpus")),
            persist=False,
            env={
                "XDG_DATA_HOME": str(tmp_path / "state"),
                "XDG_CONFIG_HOME": str(tmp_path / "config"),
            },
        )
    )
    try:
        result = await bundle.runtime._deps.external.discover(
            "document semantic search read view guide", 7
        )
        tools = {item["tool"] for item in json.loads(result.text_content())["tools"]}
        assert "documents.doc_semantic_search" in tools
        assert "documents.doc_read" in tools and "documents.doc_view" in tools
    finally:
        await bundle.runtime.close()


async def test_real_agent_loop_passes_attachment_to_stdio_mcp_then_views_original(
    tmp_path, monkeypatch
):
    root = document_library(tmp_path / "corpus")
    image = image_block()
    reference = "attachment:" + hashlib.sha256(base64.b64decode(image.data)).hexdigest()
    locator = "doc:paper_alpha@v1/marker-json-v1/unit:u_figure"
    with inference_server() as (url, _):
        index, caption, env = configuration(url)
        service = IndexService(tmp_path / "state/dcx", index, caption, env)
        library = DocumentLibrary(root)
        job = service.start(library)
        assert wait(service, library, job["id"])["status"] == "succeeded"
        config = tmp_path / "config.toml"
        config.write_text(
            "[deepdocx.index]\n"
            + "\n".join(f"{key} = {json.dumps(value)}" for key, value in asdict(index).items())
            + "\n[deepdocx.caption]\n"
            + "\n".join(f"{key} = {json.dumps(value)}" for key, value in asdict(caption).items())
        )
        fake = FakeProvider(
            [
                [
                    tool_response(
                        [
                            (
                                "discover",
                                "DiscoverTools",
                                {"query": "semantic image search", "limit": 2},
                            )
                        ]
                    )
                ],
                [
                    tool_response(
                        [
                            (
                                "search",
                                "CallExternalTool",
                                {
                                    "tool": "documents.doc_semantic_search",
                                    "arguments": {
                                        "query": "memory architecture",
                                        "image_ref": reference,
                                        "target": "image",
                                        "top_k": 3,
                                    },
                                },
                            )
                        ]
                    )
                ],
                [
                    tool_response(
                        [
                            (
                                "view",
                                "CallExternalTool",
                                {"tool": "documents.doc_view", "arguments": {"locator": locator}},
                            )
                        ]
                    )
                ],
                [text_response(f"Source verified [figure]({locator})")],
            ]
        )
        monkeypatch.setattr(
            assembly,
            "build_provider",
            lambda *args, **kwargs: (
                fake,
                ModelProfile(
                    "anthropic", "anthropic_compat", "fake", 200000, 8192, supports_images=True
                ),
            ),
        )
        bundle = build_runtime(
            BuildOptions(
                cwd=tmp_path,
                library=str(root),
                config_path=config,
                persist=False,
                memory_enabled=False,
                env={
                    **env,
                    "XDG_DATA_HOME": str(tmp_path / "state"),
                    "XDG_CONFIG_HOME": str(tmp_path / "config"),
                },
            )
        )
        try:
            events = [
                event
                async for event in bundle.runtime.run_turn(
                    "找出这张图的来源并核对原图", images=[image]
                )
            ]
            assert events[-1].stop_reason == "end_turn"
            assert all(
                request.system == fake.requests[0].system
                and request.tools == fake.requests[0].tools
                for request in fake.requests
            )
            search_result = next(
                message.tool_results()[0]
                for message in bundle.runtime.messages
                if message.tool_results() and message.tool_results()[0].tool_use_id == "search"
            )
            assert not search_result.is_error
            assert all(not isinstance(block, ImageBlock) for block in search_result.content)
            payload = json.loads(search_result.content[0].text)
            assert payload["query_mode"] == "image_text" and len(payload["hits"]) <= 3
            assert payload["coverage"]["indexed_sources"][0]["doc_id"] == "paper_alpha"
            viewed = next(
                message.tool_results()[0]
                for message in bundle.runtime.messages
                if message.tool_results() and message.tool_results()[0].tool_use_id == "view"
            )
            assert any(isinstance(block, ImageBlock) for block in viewed.content)
        finally:
            await bundle.runtime.close()
