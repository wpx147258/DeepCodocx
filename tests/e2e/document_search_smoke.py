"""Offline document chat acceptance: attachments -> MCP cards -> source reading."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import socket
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import uvicorn

from dcx.assembly import BuildOptions, build_runtime
from dcx.deepdocx.library import DocumentLibrary
from dcx.deepdocx.semantic.service import IndexService
from dcx.messages import ImageBlock
from dcx.providers.base import ModelProfile
from dcx.web.server import create_app
from tests.support.document_fixture import document_library
from tests.support.fake_provider import FakeProvider, text_response, tool_response
from tests.support.inference_fixture import inference_server
from tests.unit.test_document_attachments import image_block
from tests.unit.test_semantic_service import configuration, wait


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="dcx-document-chat-") as temporary,
        inference_server() as (url, _),
    ):
        root = Path(temporary)
        library = DocumentLibrary(document_library(root / "corpus"))
        index, caption, env = configuration(url)
        service = IndexService(root / "state/dcx", index, caption, env)
        job = service.start(library)
        assert wait(service, library, job["id"])["status"] == "succeeded"
        config = root / "config.toml"
        config.write_text(
            "[deepdocx.index]\n"
            + "\n".join(f"{k} = {json.dumps(v)}" for k, v in asdict(index).items())
            + "\n[deepdocx.caption]\n"
            + "\n".join(f"{k} = {json.dumps(v)}" for k, v in asdict(caption).items())
        )
        image = image_block()
        raw = base64.b64decode(image.data)
        reference = "attachment:" + hashlib.sha256(raw).hexdigest()
        image_path = root / "query.png"
        image_path.write_bytes(raw)
        paragraph = "doc:paper_alpha@v1/marker-json-v1/unit:u_paragraph"
        figure = "doc:paper_alpha@v1/marker-json-v1/unit:u_figure"

        def external(ident, name, arguments):
            return [
                tool_response(
                    [
                        (
                            ident,
                            "CallExternalTool",
                            {"tool": "documents." + name, "arguments": arguments},
                        )
                    ]
                )
            ]

        fake = FakeProvider(
            [
                [
                    tool_response(
                        [
                            (
                                "discover",
                                "DiscoverTools",
                                {"query": "document semantic search read view guide", "limit": 7},
                            )
                        ]
                    )
                ],
                external(
                    "text-search", "doc_semantic_search", {"query": "memory", "target": "text"}
                ),
                external("read", "doc_read", {"locator": paragraph}),
                [text_response(f"已读取原文，记忆保留观察。[原文]({paragraph})")],
                external(
                    "image-search",
                    "doc_semantic_search",
                    {"query": "memory architecture", "image_ref": reference, "target": "image"},
                ),
                external("view", "doc_view", {"locator": figure}),
                [text_response(f"已用图文混合检索并核对原图。[图示]({figure})")],
            ]
        )
        with patch(
            "dcx.assembly.build_provider",
            return_value=(
                fake,
                ModelProfile(
                    "anthropic",
                    "anthropic_compat",
                    "offline-demo",
                    200000,
                    8192,
                    supports_images=True,
                ),
            ),
        ):
            bundle = build_runtime(
                BuildOptions(
                    cwd=root,
                    library=str(library.root),
                    config_path=config,
                    memory_enabled=False,
                    env={
                        **env,
                        "XDG_DATA_HOME": str(root / "state"),
                        "XDG_CONFIG_HOME": str(root / "config"),
                    },
                )
            )
        app = create_app(bundle)
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}/")
                expect(page.locator("#semantic-status")).to_have_text("语义索引 1/1", timeout=20000)
                assert not fake.requests
                page.locator("#semantic-status").click()
                expect(page.locator("#index-dialog")).to_be_visible()
                page.locator("#index-dismiss").click()
                page.locator("#input").fill("请从库中用语义相关原文解释记忆")
                page.locator("#send").click()
                expect(page.locator("#messages")).to_contain_text("已读取原文", timeout=25000)
                expect(page.locator(".semantic-result-summary")).to_have_count(1)
                expect(page.locator(".semantic-source-card")).to_have_count(1)
                card = page.locator(".semantic-source-card").first
                card.locator("xpath=ancestor::details[contains(@class,'tool')]").evaluate(
                    "item => item.open=true"
                )
                expect(card).to_contain_text("原文片段")
                if args.output:
                    page.screenshot(
                        path=str(args.output / "document-semantic-chat.png"), animations="disabled"
                    )
                page.locator("#image-input").set_input_files(str(image_path))
                page.locator("#input").fill("结合图片和文字做图文混合检索并查看原图")
                page.locator("#send").click()
                expect(page.locator("#messages")).to_contain_text("已用图文混合检索", timeout=25000)
                expect(page.locator(".semantic-result-summary")).to_have_count(2)
                second = page.locator(".semantic-source-card").last
                second.locator("xpath=ancestor::details[contains(@class,'tool')]").evaluate(
                    "item => item.open=true"
                )
                expect(page.locator(".semantic-result-summary").last).to_contain_text(
                    "图文混合检索"
                )
                second.locator(".source-link").click()
                expect(page.locator("#evidence-panel")).to_be_visible()
                expect(page.locator("#evidence-image")).to_be_visible()
                if args.output:
                    page.screenshot(
                        path=str(args.output / "document-image-chat.png"), animations="disabled"
                    )
                page.locator("#evidence-close").click()
                page.locator("#theme-toggle").click()
                if args.output:
                    page.screenshot(
                        path=str(args.output / "document-image-chat-dark.png"),
                        animations="disabled",
                    )
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if args.output:
                    page.screenshot(
                        path=str(args.output / "document-image-chat-mobile.png"),
                        animations="disabled",
                    )
                page.reload()
                expect(page.locator(".semantic-result-summary")).to_have_count(2)
                assert all(
                    request.system == fake.requests[0].system
                    and request.tools == fake.requests[0].tools
                    for request in fake.requests
                )
                searches = [
                    result
                    for message in bundle.runtime.messages
                    for result in message.tool_results()
                    if result.tool_use_id in {"text-search", "image-search"}
                ]
                assert all(
                    not result.is_error
                    and not any(isinstance(block, ImageBlock) for block in result.content)
                    for result in searches
                )
                assert not errors, errors
                browser.close()
            print(
                "PASS: document chat discovers MCP, uploaded attachment references support mixed search, compact cards and source links render, coverage badge, dark/mobile/reload, unchanged system/tools."
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
