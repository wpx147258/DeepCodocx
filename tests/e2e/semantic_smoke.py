"""Offline acceptance of vector indexing, native joint requests and management UI."""

from __future__ import annotations

import argparse
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
from dcx.providers.base import ModelProfile
from dcx.web.server import create_app
from tests.support.document_fixture import document_library
from tests.support.fake_provider import FakeProvider
from tests.support.inference_fixture import inference_server
from tests.unit.test_semantic_service import configuration, wait


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="dcx-semantic-ui-") as temporary,
        inference_server() as (url, requests),
    ):
        root = Path(temporary)
        corpus = document_library(root / "corpus/paper-bundles")
        index, caption, env = configuration(url)
        config = root / "config.toml"
        # TOML scalar values coincide with JSON for strings, ints and booleans.
        config.write_text(
            "[deepdocx.index]\n"
            + "\n".join(f"{key} = {json.dumps(value)}" for key, value in asdict(index).items())
            + "\n[deepdocx.caption]\n"
            + "\n".join(f"{key} = {json.dumps(value)}" for key, value in asdict(caption).items())
        )
        fake = FakeProvider([])
        with patch(
            "dcx.assembly.build_provider",
            return_value=(
                fake,
                ModelProfile("anthropic", "anthropic_compat", "offline-demo", 200000, 8192),
            ),
        ):
            bundle = build_runtime(
                BuildOptions(
                    cwd=root,
                    library=str(corpus),
                    config_path=config,
                    memory_enabled=False,
                    env={
                        **env,
                        "XDG_DATA_HOME": str(root / "state"),
                        "XDG_CONFIG_HOME": str(root / "config"),
                    },
                )
            )
        app = create_app(bundle, token="browser-token")
        history = bundle.config.paths.session_dir(bundle.session_id) / "session.jsonl"
        before = history.read_bytes()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
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
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1400, "height": 1000})
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(f"http://127.0.0.1:{port}/?token=browser-token")
                expect(page.locator(".brand-name")).to_have_text("DeepDocx")
                page.locator("#index-open").click()
                expect(page.locator("#index-summary")).to_contain_text("0 / 1")
                expect(page.locator("#index-sync")).to_be_enabled()
                expect(page.locator("#index-captions")).to_be_checked()
                page.locator("#index-caption-model").select_option("DeepSeek-V4.1-Flash-1017")
                page.locator("#index-sync").click()
                expect(page.locator("#index-job-status")).to_have_text("同步完成", timeout=25000)
                expect(page.locator("#index-summary")).to_contain_text("1 / 1")

                def missing_caption(route):
                    payload = route.fetch().json()
                    payload["caption_ready"] = False
                    route.fulfill(json=payload)

                page.route("**/api/index/status?**", missing_caption)
                page.locator("#index-refresh").click()
                expect(page.locator("#index-captions")).not_to_be_checked()
                expect(page.locator("#index-captions")).to_be_disabled()
                expect(page.locator("#index-sync")).to_be_enabled()
                page.unroute("**/api/index/status?**")
                page.locator("#index-refresh").click()
                expect(page.locator("#index-captions")).to_be_checked()
                expect(page.locator("#index-captions")).to_be_enabled()
                expect(page.locator("#index-coverage strong")).to_have_text(["1", "1", "2", "2"])
                if args.output:
                    page.screenshot(
                        path=str(args.output / "deepdocx-index-light.png"), animations="disabled"
                    )
                page.locator(".index-preview summary").click()
                page.locator("#index-query").fill("memory architecture")
                page.locator("#index-search").click()
                expect(page.locator(".index-result")).to_have_count(2)
                page.locator(".index-result button").first.click()
                expect(page.locator("#index-dialog")).not_to_be_visible()
                expect(page.locator("#evidence-panel")).to_be_visible()
                page.locator("#index-open").click()
                expect(page.locator("#index-summary")).to_contain_text("1 / 1")
                calls = len(requests)
                with page.expect_response("**/api/index/sync") as response:
                    page.locator("#index-sync").click()
                job = response.value.json()
                assert (
                    wait(app.state.host.indexes, bundle.document_library, job["id"])["status"]
                    == "succeeded"
                )
                page.locator("#index-refresh").click()
                expect(page.locator("#index-job-status")).to_have_text("同步完成", timeout=20000)
                assert len(requests) == calls
                page.evaluate("document.documentElement.dataset.theme='dark'")
                if args.output:
                    page.screenshot(
                        path=str(args.output / "deepdocx-index-dark.png"), animations="disabled"
                    )
                page.locator(".index-preview summary").click()
                page.locator(".index-body").evaluate("node => node.scrollTop = 0")
                page.set_viewport_size({"width": 390, "height": 800})
                expect(page.locator("#index-sync")).to_be_in_viewport()
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                if args.output:
                    page.screenshot(
                        path=str(args.output / "deepdocx-index-mobile.png"), animations="disabled"
                    )
                page.locator("#index-dismiss").click()
                page.reload()
                page.locator("#menu").click()
                expect(page.locator("#mode-toggle")).to_be_in_viewport()
                page.locator("#index-open").click()
                expect(page.locator("#index-summary")).to_contain_text("1 / 1")
                assert any(
                    "image" in item and "text" in item
                    for request in requests
                    if request["path"].endswith("/embeddings")
                    for item in request["body"]["input"]
                )
                assert not fake.requests and history.read_bytes() == before
                assert not errors, errors
                browser.close()
            print(
                "PASS: authenticated index UI, six representations/four families, caption model selection, actual background jobs and SQLite cosine, native multimodal requests, source preview, cached resync, reload/dark/mobile; zero agent model calls or transcript edits"
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("test server did not exit")


if __name__ == "__main__":
    main()
