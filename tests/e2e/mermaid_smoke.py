"""Offline browser acceptance: python -m tests.e2e.mermaid_smoke [--output DIR]."""

from __future__ import annotations

import argparse
import json
import socket
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import uvicorn

from dcx.assembly import BuildOptions, build_runtime
from dcx.messages import TextBlock, assistant_message, user_message
from dcx.providers.base import ModelProfile, TextDelta
from dcx.web.server import create_app
from tests.support.document_fixture import document_library
from tests.support.fake_provider import FakeProvider, text_response

FLOW = """flowchart LR
    subgraph Host[CLI / Web]
        U[用户输入] --> M[消息]
    end
    M --> A[Agent loop]
    A --> T[工具执行]
    T --> A
    A --> R[回答]
"""
SEQUENCE = """sequenceDiagram
    participant User
    participant Agent
    User->>Agent: Explain this paper
    Note over Agent: Read original evidence
    Agent-->>User: Answer with sources
"""
TALL = "flowchart TD\n" + "\n".join(f"N{i}[Step {i}] --> N{i + 1}" for i in range(24))
MARKDOWN = (
    "A message travels through the workspace.\n\n```mermaid\n" + FLOW + "```\n\n"
    "```mermaid\n" + SEQUENCE + "```\n\n"
    "```mermaid\n" + TALL + "\n```\n\n"
    "```mermaid\nthis is not a valid diagram\n```\n\n"
    "```python\nprint('ordinary code')\n```"
)


def main() -> None:
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)

    def screenshot(page, name):
        if args.output:
            page.screenshot(path=str(args.output / name), animations="disabled")

    with tempfile.TemporaryDirectory(prefix="dcx-mermaid-") as temporary:
        root = Path(temporary)
        corpus = document_library(root / "corpus/paper-bundles")
        config = root / "config.toml"
        config.write_text('[deepdocx]\nlibraries = [' + json.dumps(str(corpus)) + ']\n')
        options = BuildOptions(
            cwd=root,
            config_path=config,
            memory_enabled=False,
            env={"XDG_DATA_HOME": str(root / "state"), "XDG_CONFIG_HOME": str(root / "config")},
        )
        stream = "```mermaid\n" + FLOW + "```"
        fake = FakeProvider([[TextDelta(stream[:28]), TextDelta(stream[28:]), text_response(stream)]])
        with patch(
            "dcx.assembly.build_provider",
            return_value=(fake, ModelProfile("anthropic", "anthropic_compat", "offline-demo", 200000, 8192)),
        ):
            bundle = build_runtime(options)
            bundle.runtime._append(user_message("Draw the lifecycle of a message."))
            bundle.runtime._append(assistant_message([TextBlock(MARKDOWN)]))
            history = bundle.config.paths.session_dir(bundle.session_id) / "session.jsonl"
            before = history.read_bytes()
            app = create_app(bundle, factory=lambda ident: build_runtime(replace(options, resume_id=ident)))
            with socket.socket() as available:
                available.bind(("127.0.0.1", 0))
                port = available.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.05)
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    context = browser.new_context(
                        viewport={"width": 1400, "height": 1000},
                        permissions=["clipboard-read", "clipboard-write"],
                    )
                    page = context.new_page()
                    errors, cancelled = [], []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("request", lambda request: cancelled.append(request.url) if request.url.endswith("/api/cancel") else None)
                    page.goto(f"http://127.0.0.1:{port}/")
                    blocks = page.locator(".mermaid-block")
                    expect(blocks).to_have_count(4)
                    expect(page.locator('.mermaid-block[data-mermaid-state="done"]')).to_have_count(3, timeout=20000)
                    broken = blocks.nth(3)
                    expect(broken.locator(".mermaid-error")).to_be_visible()
                    expect(broken.locator('[data-diagram-view="diagram"]')).to_be_disabled()
                    expect(broken.locator(".mermaid-source")).to_contain_text("not a valid diagram")
                    expect(page.locator(".code:not(.mermaid-block) pre")).to_contain_text("ordinary code")

                    block = blocks.first
                    block.scroll_into_view_if_needed()
                    svg_id = block.locator("svg").get_attribute("id")
                    block.locator('[data-diagram-view="source"]').click()
                    expect(block.locator(".mermaid-source")).to_have_text(FLOW.rstrip())
                    expect(block.locator(".mermaid-canvas")).to_be_hidden()
                    block.locator(".mermaid-copy").click()
                    expect(block.locator(".mermaid-copy")).to_have_text("Copied")
                    assert page.evaluate("navigator.clipboard.readText()") == FLOW.rstrip()
                    block.locator('[data-diagram-view="diagram"]').click()
                    expect(block.locator(".mermaid-canvas")).to_be_visible()
                    assert block.locator("svg").get_attribute("id") == svg_id
                    assert block.locator(".code-head").evaluate("e => getComputedStyle(e).backgroundColor") != "rgb(32, 31, 28)"
                    screenshot(page, "mermaid-light.png")

                    block.locator(".mermaid-canvas").focus()
                    page.keyboard.press("Enter")
                    viewer = page.locator("#diagram-viewer")
                    expect(viewer).to_be_visible()
                    page.wait_for_function("document.getElementById('diagram-image').naturalWidth > 0")
                    width = page.locator("#diagram-image").evaluate("e => e.width")
                    page.locator("#diagram-zoom-in").click()
                    assert page.locator("#diagram-image").evaluate("e => e.width") > width
                    screenshot(page, "mermaid-viewer-light.png")
                    page.keyboard.press("Escape")
                    expect(viewer).not_to_be_visible()
                    expect(block.locator(".mermaid-canvas")).to_be_focused()
                    assert not cancelled

                    # A tall diagram must be readable at actual size and pannable.
                    tall = blocks.nth(2)
                    tall.locator(".mermaid-expand").click()
                    page.locator("#diagram-actual").click()
                    viewport = page.locator("#diagram-viewport")
                    assert viewport.evaluate("e => e.scrollHeight > e.clientHeight")
                    top = viewport.evaluate("e => e.scrollTop")
                    rect = viewport.bounding_box()
                    page.mouse.move(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
                    page.mouse.down()
                    page.mouse.move(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2 - 90, steps=5)
                    page.mouse.up()
                    assert viewport.evaluate("e => e.scrollTop") > top
                    page.locator("#diagram-fit").click()
                    assert viewport.evaluate("e => e.scrollHeight <= e.clientHeight + 1")
                    page.locator("#diagram-close").click()

                    # Keep source selection through a theme change and queued render.
                    block.locator('[data-diagram-view="source"]').click()
                    page.locator("#theme-toggle").click()
                    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                    expect(page.locator('.mermaid-block[data-mermaid-state="done"]')).to_have_count(3, timeout=20000)
                    expect(block.locator(".mermaid-source")).to_be_visible()
                    expect(block.locator('[data-diagram-view="source"]')).to_have_attribute("aria-pressed", "true")
                    block.locator('[data-diagram-view="diagram"]').click()
                    block.scroll_into_view_if_needed()
                    screenshot(page, "mermaid-dark.png")
                    assert history.read_bytes() == before

                    # Invalidate an in-flight render twice; the latest theme wins.
                    page.evaluate("""() => {
                        const original = mermaid.render;
                        window.restoreMermaid = () => { mermaid.render = original; };
                        mermaid.render = async (...args) => {
                            await new Promise(resolve => setTimeout(resolve, 60));
                            return original(...args);
                        };
                    }""")
                    page.locator("#theme-toggle").click()
                    page.locator("#theme-toggle").click()
                    expect(page.locator('.mermaid-block[data-mermaid-state="done"]')).to_have_count(3, timeout=20000)
                    assert block.locator(".node rect").first.evaluate("e => getComputedStyle(e).fill") == "rgb(58, 42, 32)"
                    page.evaluate("restoreMermaid()")

                    # Streamed output, then restore it from append-only history.
                    page.locator("#input").fill("Draw it again.")
                    page.locator("#send").click()
                    expect(blocks).to_have_count(5)
                    expect(blocks.last).to_have_attribute("data-mermaid-state", "done", timeout=20000)
                    page.reload()
                    expect(blocks).to_have_count(5)
                    expect(blocks.last).to_have_attribute("data-mermaid-state", "done", timeout=20000)
                    assert len(fake.requests) == 1

                    page.locator("#deepdocx-entry").click()
                    page.locator('input[name="docx-start"][value="fork"]').check()
                    page.locator("#deepdocx-enter").click()
                    expect(page.locator("html")).to_have_attribute("data-mode", "deepdocx")
                    expect(blocks.first).to_have_attribute("data-mermaid-state", "done", timeout=20000)
                    blocks.first.scroll_into_view_if_needed()
                    screenshot(page, "mermaid-deepdocx-dark.png")
                    page.locator("#theme-toggle").click()
                    expect(blocks.first).to_have_attribute("data-mermaid-state", "done", timeout=20000)
                    blocks.first.locator(".mermaid-expand").click()
                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(viewer).to_be_visible()
                    expect(page.locator("#diagram-close")).to_be_in_viewport()
                    expect(page.locator("#diagram-fit")).to_be_in_viewport()
                    page.wait_for_function("document.getElementById('diagram-viewport').scrollWidth <= document.getElementById('diagram-viewport').clientWidth + 1")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    screenshot(page, "mermaid-viewer-mobile.png")
                    page.mouse.click(2, 2)
                    expect(viewer).not_to_be_visible()
                    blocks.first.locator(".mermaid-expand").click()
                    page.locator("#diagram-close").click()
                    blocks.first.scroll_into_view_if_needed()
                    screenshot(page, "mermaid-mobile.png")
                    assert not errors, errors
                    browser.close()
                print("PASS: Mermaid source/diagram switching, exact copy, themed SVG, viewer zoom/pan/fit/Escape, error fallback, stream/reload, DeepDocx and mobile; no browser errors")
            finally:
                server.should_exit = True
                thread.join(timeout=10)


if __name__ == "__main__":
    main()
