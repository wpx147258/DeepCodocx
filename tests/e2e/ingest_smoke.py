"""Offline ingestion acceptance: python -m tests.e2e.ingest_smoke [--output DIR].

Uses Chromium, a real host worker and Poppler on an isolated tiny PDF. No model
requests, personal corpus writes or Marker model downloads are needed.
"""

from __future__ import annotations

import argparse
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import uvicorn

from dcx.assembly import BuildOptions, build_runtime
from dcx.deepdocx.library import DocumentLibrary
from dcx.providers.base import ModelProfile
from dcx.web.server import create_app
from tests.support.document_fixture import document_library
from tests.support.fake_provider import FakeProvider
from tests.support.pdf_fixture import small_pdf


def main() -> None:
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output
    if output:
        output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dcx-ingest-browser-") as temporary:
        root = Path(temporary)
        corpus = document_library(root / "papers" / "paper-bundles")
        config = root / "settings.toml"
        config.write_text(
            '[deepdocx.ingest]\nmarker_command = "/missing/marker"\nmax_files = 2\nmax_upload_mb = 1\n'
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
                    config_path=config,
                    library=str(corpus),
                    memory_enabled=False,
                    env={
                        "XDG_DATA_HOME": str(root / "state"),
                        "XDG_CONFIG_HOME": str(root / "config"),
                    },
                )
            )
        app = create_app(bundle)
        host = app.state.host
        session_path = bundle.config.paths.session_dir(bundle.session_id) / "session.jsonl"
        original_history = session_path.read_bytes()
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
                page = browser.new_page(viewport={"width": 1280, "height": 720})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                url = f"http://127.0.0.1:{port}"
                page.goto(url)
                expect(page.locator(".brand-name")).to_have_text("DeepDocx")
                # A long corpus must scroll independently of permission controls.
                docs = DocumentLibrary(corpus).documents()
                many = [
                    {
                        **docs[0],
                        "doc_id": f"paper_{i}",
                        "title": f"Paper {i}: Memory and visual action",
                    }
                    for i in range(76)
                ]
                page.route(
                    "**/api/documents?**", lambda route: route.fulfill(json={"documents": many})
                )
                page.reload()
                expect(page.locator(".document-item")).to_have_count(76)
                for size in ((1280, 720), (1000, 600), (800, 600), (390, 700)):
                    page.set_viewport_size({"width": size[0], "height": size[1]})
                    if size[0] <= 900:
                        page.locator("#menu").click()
                    expect(page.locator("#mode-toggle")).to_be_in_viewport()
                    expect(page.locator("#model-select")).to_be_in_viewport()
                    expect(page.locator("#ctx-text")).to_be_in_viewport()
                    page.locator(".side-scroll").evaluate(
                        "node => node.scrollTop = node.scrollHeight"
                    )
                    expect(page.locator("#mode-toggle")).to_be_in_viewport()
                    if output and size == (1280, 720):
                        page.locator(".side-scroll").evaluate("node => node.scrollTop = 0")
                        page.screenshot(
                            path=str(output / "deepdocx-sidebar.png"), animations="disabled"
                        )
                    if size[0] <= 900:
                        page.locator("#sidebar-close").click()
                    page.locator(".side-scroll").evaluate("node => node.scrollTop = 0")
                page.set_viewport_size({"width": 1280, "height": 900})
                page.unroute("**/api/documents?**")
                page.reload()
                page.locator("#ingest-open").click()
                expect(page.locator("#ingest-env")).to_contain_text("转换在后台执行")
                expect(page.locator("#ingest-start")).to_be_disabled()
                expect(page.locator('input[name="ingest-mode"][value="marker"]')).to_be_disabled()
                expect(page.locator('input[name="ingest-mode"][value="both"]')).to_be_disabled()
                assert page.locator("#ingest-force").bounding_box()["width"] <= 20
                if output:
                    page.screenshot(path=str(output / "deepdocx-ingest.png"), animations="disabled")
                    page.evaluate("document.documentElement.dataset.theme = 'dark'")
                    page.screenshot(
                        path=str(output / "deepdocx-ingest-dark.png"), animations="disabled"
                    )
                    page.evaluate("document.documentElement.dataset.theme = 'light'")
                # The same compact dialog must fit mobile and a short viewport.
                page.set_viewport_size({"width": 390, "height": 700})
                expect(page.locator("#ingest-start")).to_be_in_viewport()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if output:
                    page.screenshot(
                        path=str(output / "deepdocx-ingest-mobile.png"), animations="disabled"
                    )
                page.set_viewport_size({"width": 1280, "height": 900})
                page.locator("#ingest-files").set_input_files(
                    {"name": "bad.txt", "mimeType": "text/plain", "buffer": b"bad"}
                )
                expect(page.locator("#ingest-error")).to_contain_text("仅支持 PDF")
                expect(page.locator("#ingest-start")).to_be_disabled()
                page.get_by_role("button", name="移除 bad.txt").click()
                page.locator("#ingest-files").set_input_files(
                    {
                        "name": "large.pdf",
                        "mimeType": "application/pdf",
                        "buffer": b"%PDF-" + b"x" * 1024 * 1024,
                    }
                )
                expect(page.locator("#ingest-error")).to_contain_text("超过")
                page.get_by_role("button", name="移除 large.pdf").click()
                page.locator("#ingest-close").click()
                # An environment request failure must fail closed and be retryable.
                page.route("**/api/ingest/environment", lambda route: route.abort())
                page.locator("#ingest-open").click()
                expect(page.locator("#ingest-env")).to_contain_text("无法检查")
                page.locator("#ingest-files").set_input_files(
                    {"name": "one.pdf", "mimeType": "application/pdf", "buffer": small_pdf()}
                )
                expect(page.locator("#ingest-start")).to_be_disabled()
                page.unroute("**/api/ingest/environment")
                page.locator("#ingest-retry").click()
                expect(page.locator("#ingest-start")).to_be_enabled()
                # Check all capability combinations, including Both's two dependencies.
                for basic, marker, selected in (
                    (False, True, "marker"),
                    (False, False, None),
                    (True, True, "basic"),
                ):
                    env = {
                        "basic_ready": basic,
                        "marker_ready": marker,
                        "basic_reason": "" if basic else "Basic unavailable",
                        "marker_reason": "" if marker else "Marker unavailable",
                        "max_files": 2,
                        "max_upload_mb": 1,
                    }
                    page.route(
                        "**/api/ingest/environment",
                        lambda route, request, env=env: route.fulfill(json=env),
                    )
                    page.locator("#ingest-retry").evaluate("node => node.click()")
                    if selected:
                        expect(
                            page.locator(f'input[name="ingest-mode"][value="{selected}"]')
                        ).to_be_checked()
                    else:
                        expect(page.locator("#ingest-start")).to_be_disabled()
                    expect(
                        page.locator('input[name="ingest-mode"][value="both"]')
                    ).to_be_enabled() if basic and marker else expect(
                        page.locator('input[name="ingest-mode"][value="both"]')
                    ).to_be_disabled()
                    page.unroute("**/api/ingest/environment")
                page.locator("#ingest-retry").evaluate("node => node.click()")
                expect(page.locator("#ingest-start")).to_be_enabled()
                page.locator("#ingest-files").set_input_files(
                    [
                        {"name": "two.pdf", "mimeType": "application/pdf", "buffer": small_pdf()},
                        {"name": "three.pdf", "mimeType": "application/pdf", "buffer": small_pdf()},
                    ]
                )
                expect(page.locator("#ingest-error")).to_contain_text("最多添加 2")
                expect(page.locator("#ingest-start")).to_be_disabled()
                page.get_by_role("button", name="移除 two.pdf").click()
                page.get_by_role("button", name="移除 three.pdf").click()
                # Start the actual host worker, leave the dialog, then recover it.
                page.locator("#ingest-start").click()
                page.locator("#ingest-form").dispatch_event("submit")
                expect(page.locator("#ingest-progress")).to_be_visible()
                page.locator("#ingest-dismiss").click()
                page.locator("#ingest-open").click()
                expect(page.locator("#ingest-status")).to_have_text("文档已添加", timeout=20000)
                expect(page.locator(".document-item")).to_have_count(2)
                expect(page.locator("#ingest-new")).to_be_enabled()
                jobs = host.ingest.list()
                assert len(jobs) == 1 and jobs[0]["status"] == "succeeded"
                assert DocumentLibrary(corpus).search("acceptance")["hits"]
                page.reload()
                page.locator("#ingest-open").click()
                # Recover an active server-side job after a browser reload.
                pending = host.ingest.create_job(bundle.libraries.selected_id(), corpus, "basic")
                host.ingest.add_file(pending["id"], "pending.pdf", small_pdf())
                page.locator("#ingest-dismiss").click()
                page.reload()
                page.locator("#ingest-open").click()
                expect(page.locator("#ingest-status")).to_have_text("正在上传")
                page.locator("#ingest-cancel").click()
                expect(page.locator("#ingest-status")).to_have_text("任务已停止")
                assert host.ingest.get(pending["id"])["status"] == "cancelled"
                page.locator("#ingest-new").click()
                expect(page.locator("#ingest-selected")).to_be_hidden()
                assert not fake.requests, "ingestion must not call a model"
                assert session_path.read_bytes() == original_history, (
                    "ingestion must not append to agent history"
                )
                assert not errors, errors
                browser.close()
            print(
                "PASS: long-library sidebar, mobile dialog, environment failures/capability combinations, file limits, actual PDF upload/build/index, close/reopen/reload/cancel; no model calls or transcript changes"
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("Browser test server did not stop")


if __name__ == "__main__":
    main()
