"""Offline browser acceptance: python -m tests.e2e.web_smoke [--output DIR].

Requires the optional Playwright test environment and installed Chromium.
Uses the real runtime, stdio MCP and HTTP server with a scripted provider.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import uvicorn

from dcx.assembly import BuildOptions, build_runtime
from dcx.deepdocx.library import DocumentLibrary
from dcx.messages import user_message
from dcx.providers.base import ModelProfile
from dcx.web.server import create_app
from tests.support.document_fixture import document_library
from tests.support.fake_provider import FakeProvider, text_response, tool_response


def _check_storage_migration(browser, url, expect) -> None:
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    headers = []
    page.on(
        "request",
        lambda request: headers.append(request.headers)
        if request.url.endswith("/api/bootstrap") else None,
    )
    page.goto(url)
    page.evaluate("""() => {
        localStorage.clear();
        sessionStorage.clear();
        localStorage.setItem('cli-agent-draft', 'Draft saved before the rename');
        localStorage.setItem('cli-agent-theme', 'dark');
        localStorage.setItem('cli-agent-sidebar-width', '320');
        localStorage.setItem('cli-agent-sidebar-collapsed', '1');
        sessionStorage.setItem('cli-agent-token', 'saved-token');
    }""")
    page.reload()
    expect(page.locator("#input")).to_have_value("Draft saved before the rename")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(page.locator("body")).to_have_class(re.compile("sidebar-collapsed"))
    assert page.evaluate("document.documentElement.style.getPropertyValue('--sidebar-w')") == "320px"
    assert page.evaluate("sessionStorage.getItem('dcx-token')") == "saved-token"
    assert headers[-1].get("x-dcx-token") == "saved-token"
    assert page.evaluate("""() =>
        !Object.keys(localStorage).some(key => key.startsWith('cli-agent-')) &&
        !Object.keys(sessionStorage).some(key => key.startsWith('cli-agent-'))
    """)
    page.locator("#input").fill("")
    page.evaluate("""() => {
        localStorage.setItem('dcx-theme', 'light');
        localStorage.setItem('cli-agent-theme', 'dark');
        sessionStorage.setItem('dcx-token', 'current-token');
        sessionStorage.setItem('cli-agent-token', 'stale-token');
    }""")
    page.reload()
    expect(page.locator("#input")).to_have_value("")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    assert page.evaluate("sessionStorage.getItem('dcx-token')") == "current-token"
    assert headers[-1].get("x-dcx-token") == "current-token"
    page.close()


def main() -> None:
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--library", type=Path, help="Optional real bundle root; accessed read-only"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="deepdocx-browser-") as temporary:
        root = Path(temporary)
        corpus = (
            args.library.resolve()
            if args.library
            else document_library(root / "corpus/paper-bundles")
        )
        library = DocumentLibrary(corpus)
        expected_documents = len(library.documents())
        config = root / "settings.toml"
        config.write_text("[deepdocx]\nlibraries = [" + json.dumps(str(corpus)) + "]\n")
        options = BuildOptions(
            cwd=root,
            env={"XDG_DATA_HOME": str(root / "state"), "XDG_CONFIG_HOME": str(root / "config")},
            memory_enabled=False,
            config_path=config,
        )
        locator = "doc:paper_alpha@v1/marker-json-v1/unit:u_figure"
        expected_text = "memory architecture"
        if args.library:
            hits = library.search("memory", doc="paper_6d40ee70d189", kind="figure")["hits"]
            if not hits:
                raise ValueError("Real browser corpus must include MEMORYVLA Marker figures")
            locator = hits[0]["locator"]
            expected_text = library.read(locator)["text"][:30]

        def provider(*args, **kwargs):
            return FakeProvider(
                [
                    [
                        tool_response(
                            [("discover", "DiscoverTools", {"query": "document search view"})]
                        )
                    ],
                    [
                        tool_response(
                            [
                                (
                                    "search",
                                    "CallExternalTool",
                                    {
                                        "tool": "documents.doc_search",
                                        "arguments": {"query": "memory"},
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
                                    {
                                        "tool": "documents.doc_view",
                                        "arguments": {"locator": locator},
                                    },
                                )
                            ]
                        )
                    ],
                    [
                        text_response(
                            f"Memory preserves observations for later action. [论文图示]({locator})"
                        )
                    ],
                ]
            ), ModelProfile(
                "anthropic", "anthropic_compat", "offline-demo", 200000, 8192, supports_images=True
            )

        with patch("dcx.assembly.build_provider", provider):
            bundle = build_runtime(options)
            bundle.runtime._append(user_message("Earlier discussion about memory."))
            original_id = bundle.session_id
            app = create_app(
                bundle, factory=lambda ident: build_runtime(replace(options, resume_id=ident))
            )
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
            errors: list[str] = []
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    _check_storage_migration(browser, f"http://127.0.0.1:{port}", expect)
                    page = browser.new_page(viewport={"width": 1500, "height": 1000})
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{port}")
                    expect(page.locator(".brand-name")).to_have_text("dcx")
                    expect(page.locator("#messages")).to_contain_text("Earlier discussion about memory.")
                    if args.output:
                        args.output.mkdir(parents=True, exist_ok=True)
                        page.screenshot(
                            path=str(args.output / "chat-light.png"), animations="disabled"
                        )
                    page.route(
                        "**/api/libraries",
                        lambda route: route.fulfill(
                            json={"libraries": [], "selected_library_id": ""}
                        ),
                    )
                    page.locator("#deepdocx-entry").click()
                    expect(page.locator("#deepdocx-error")).to_contain_text("未找到可用文档库")
                    expect(page.locator("#deepdocx-enter")).to_be_disabled()
                    expect(page.locator("#deepdocx-library")).to_be_disabled()
                    page.locator("#deepdocx-cancel").click()
                    page.unroute("**/api/libraries")
                    page.locator("#deepdocx-entry").click()
                    expect(page.locator("#deepdocx-dialog")).to_be_visible()
                    expect(page.locator("#deepdocx-library option")).to_have_count(1)
                    expect(page.locator("#deepdocx-enter")).to_be_enabled()
                    initial_library = page.locator("#deepdocx-library").input_value()
                    assert initial_library
                    page.locator('input[name="docx-start"][value="fork"]').check()
                    page.locator("#deepdocx-enter").click()
                    expect(page.locator(".brand-name")).to_have_text("DeepDocx")
                    expect(page.locator("#document-list button")).to_have_count(expected_documents)
                    current = page.evaluate("fetch('/api/bootstrap').then(r=>r.json())")
                    assert (
                        current["session_id"] != original_id
                        and current["session_context"]["fork_boundary"] == 1
                    )
                    page.locator("#input").fill("解释论文中的记忆架构，并查看原图")
                    page.locator("#send").click()
                    expect(page.locator("#messages")).to_contain_text(
                        "Memory preserves observations", timeout=20000
                    )
                    expect(page.locator("#messages a.source-link")).to_have_count(1)
                    page.locator("#messages a.source-link").click()
                    expect(page.locator("#evidence-panel")).to_be_visible()
                    expect(page.locator("#evidence-image")).to_be_visible()
                    expect(page.locator("#evidence-text")).to_contain_text(expected_text)
                    assert page.locator("#evidence-image").evaluate("image=>image.naturalWidth") > 0
                    if args.output:
                        page.screenshot(
                            path=str(args.output / "deepdocx-light.png"), animations="disabled"
                        )
                    page.reload()
                    expect(page.locator(".brand-name")).to_have_text("DeepDocx")
                    page.locator("#deepdocx-entry").click()
                    expect(page.locator(".brand-name")).to_have_text("dcx")
                    sessions = page.evaluate("fetch('/api/sessions').then(r=>r.json())")
                    assert [item["id"] for item in sessions["sessions"]] == [original_id]
                    page.locator("#deepdocx-entry").click()
                    expect(page.locator("#deepdocx-library option")).to_have_count(1)
                    expect(page.locator("#deepdocx-library")).to_have_value(initial_library)
                    page.locator("#deepdocx-enter").click()
                    expect(page.locator(".brand-name")).to_have_text("DeepDocx")
                    fresh = page.evaluate("fetch('/api/bootstrap').then(r=>r.json())")
                    assert fresh["messages"] == []
                    page.locator("#theme-toggle").click()
                    if args.output:
                        page.screenshot(
                            path=str(args.output / "deepdocx-dark.png"), animations="disabled"
                        )
                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(page.locator("#deepdocx-entry")).to_be_visible()
                    for _ in range(20):
                        if (
                            page.locator(".sidebar").evaluate(
                                "node => node.getBoundingClientRect().right"
                            )
                            <= 0
                        ):
                            break
                        time.sleep(0.05)
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth + 1"
                    )
                    if args.output:
                        page.screenshot(
                            path=str(args.output / "deepdocx-mobile.png"), animations="disabled"
                        )
                    page.set_viewport_size({"width": 1500, "height": 1000})
                    page.locator("#deepdocx-entry").click()
                    expect(page.locator(".brand-name")).to_have_text("dcx")
                    page.locator("#deepdocx-entry").click()
                    second = document_library(root / "second-library")
                    if not page.locator(".library-register").evaluate("node => node.open"):
                        page.locator(".library-register summary").click()
                    page.locator("#deepdocx-root").fill(str(second))
                    page.locator("#deepdocx-register").click()
                    expect(page.locator("#deepdocx-library option")).to_have_count(2)
                    chosen_library = page.locator("#deepdocx-library").input_value()
                    assert chosen_library != initial_library
                    page.locator("#deepdocx-enter").click()
                    expect(page.locator(".brand-name")).to_have_text("DeepDocx")
                    page.locator("#deepdocx-entry").click()
                    expect(page.locator(".brand-name")).to_have_text("dcx")
                    page.reload()
                    page.locator("#deepdocx-entry").click()
                    expect(page.locator("#deepdocx-library")).to_have_value(chosen_library)
                    page.locator("#deepdocx-cancel").click()
                    assert not errors, errors
                    browser.close()
                print(
                    "PASS: storage migration, dcx branding, default library, empty-state guidance, remembered library selection, fork/new, stdio MCP discovery/search/image, citations, reload, history isolation, dark theme and mobile; no browser errors"
                )
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                if thread.is_alive():
                    raise RuntimeError("Browser test server did not stop")


if __name__ == "__main__":
    main()
