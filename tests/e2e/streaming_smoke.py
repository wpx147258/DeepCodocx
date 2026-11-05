"""Offline browser acceptance: reload mid-stream and retry-note consolidation.

Run with the optional Playwright environment:
    uv run --with playwright python -m tests.e2e.streaming_smoke
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import uvicorn

from dcx.assembly import BuildOptions, build_runtime
from dcx.messages import TextBlock, Usage, assistant_message
from dcx.providers.base import (
    ModelProfile,
    ProviderError,
    ResponseCompleted,
    TextDelta,
)
from dcx.web.server import create_app

PROFILE = ModelProfile("mock", "anthropic_compat", "mock-model", 200_000, 8192)


class SlowProvider:
    """Send an opening delta, pause, then finish after a reload can occur."""

    async def stream(self, request):
        yield TextDelta("Hello")
        await asyncio.sleep(4)
        yield TextDelta(" world")
        yield ResponseCompleted(
            message=assistant_message([TextBlock("Hello world")]),
            stop_reason="end_turn",
            raw_stop_reason="end_turn",
            usage=Usage(input_tokens=3, output_tokens=2),
        )

    async def aclose(self) -> None:
        return None


MATH = r"""Inline $E = mc^2$, \(a^2 + b^2 = c^2\), and $a*b*c$.

$$
\int_0^1 x^2\,dx = \frac{1}{3}
$$

Display \[ \sum_{i=1}^{n} i = \frac{n(n+1)}{2} \] is also supported.
"""


class MathProvider:
    async def stream(self, request):
        yield ResponseCompleted(
            message=assistant_message([TextBlock(MATH)]),
            stop_reason="end_turn",
            raw_stop_reason="end_turn",
            usage=Usage(input_tokens=3, output_tokens=2),
        )

    async def aclose(self) -> None:
        return None


class RetryProvider:
    """Fail twice with retryable errors, then stream a final answer."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request):
        self.calls += 1
        if self.calls < 3:
            yield ProviderError("temporary network failure", "retryable")
            return
        yield TextDelta("Recovered")
        await asyncio.sleep(3)
        yield ResponseCompleted(
            message=assistant_message([TextBlock("Recovered")]),
            stop_reason="end_turn",
            raw_stop_reason="end_turn",
            usage=Usage(input_tokens=3, output_tokens=2),
        )

    async def aclose(self) -> None:
        return None


def _start(provider, root: Path):
    config = root / "settings.toml"
    config.write_text("", encoding="utf-8")
    options = BuildOptions(
        cwd=root,
        env={"XDG_DATA_HOME": str(root / "state"), "XDG_CONFIG_HOME": str(root / "config")},
        memory_enabled=False,
        config_path=config,
    )

    def factory(*args, **kwargs):
        return provider, PROFILE

    with patch("dcx.assembly.build_provider", factory):
        bundle = build_runtime(options)
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
    return server, thread, f"http://127.0.0.1:{port}/"


def _stop(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        raise RuntimeError("Browser test server did not stop")


def main() -> None:
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output
    if output:
        output.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()

        with tempfile.TemporaryDirectory(prefix="dcx-stream-") as temporary:
            server, thread, url = _start(SlowProvider(), Path(temporary))
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                page.locator("#input").fill("stream slowly")
                page.locator("#send").click()
                expect(page.locator("#messages")).to_contain_text("Hello", timeout=5000)
                page.reload()
                # The reloaded page must reattach and replay the partial answer.
                expect(page.locator("#messages")).to_contain_text("Hello", timeout=5000)
                expect(page.locator("#messages")).to_contain_text(
                    "Hello world", timeout=10000
                )
                expect(page.locator(".msg-user")).to_have_count(1)
                if output:
                    page.screenshot(
                        path=str(output / "stream-reload.png"), animations="disabled"
                    )
                assert not errors, errors
                page.close()
            finally:
                _stop(server, thread)

        with tempfile.TemporaryDirectory(prefix="dcx-retry-") as temporary:
            provider = RetryProvider()
            server, thread, url = _start(provider, Path(temporary))
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                page.locator("#input").fill("retry please")
                page.locator("#send").click()
                expect(page.locator(".retry-note")).to_have_count(1, timeout=6000)
                expect(page.locator(".retry-note")).to_contain_text(
                    "2/3", timeout=7000
                )
                expect(page.locator("#messages")).to_contain_text(
                    "Recovered", timeout=10000
                )
                expect(page.locator(".retry-note")).to_have_count(0, timeout=5000)
                if output:
                    page.screenshot(
                        path=str(output / "stream-retry.png"), animations="disabled"
                    )
                assert not errors, errors
                page.close()
            finally:
                _stop(server, thread)

        with tempfile.TemporaryDirectory(prefix="dcx-math-") as temporary:
            server, thread, url = _start(MathProvider(), Path(temporary))
            try:
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    permissions=["clipboard-read", "clipboard-write"],
                )
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                page.locator("#input").fill("render $x^2$ formulas")
                page.locator("#send").click()
                expect(page.locator(".msg-user .katex")).to_have_count(1)
                expect(page.locator(".katex")).to_have_count(6, timeout=10000)
                expect(page.locator(".katex-display")).to_have_count(2)
                expect(page.locator("#messages")).not_to_contain_text("$E =")
                expect(page.locator(".copy-response")).to_have_count(1)
                page.locator(".copy-response").first.click()
                expect(page.locator(".copy-response.copied")).to_have_count(1)
                assert "E = mc^2" in page.evaluate("navigator.clipboard.readText()")

                # Every completed assistant turn, including earlier ones, keeps
                # its own copy tag.
                page.locator("#input").fill("second response")
                page.locator("#send").click()
                expect(page.locator(".copy-response")).to_have_count(2, timeout=10000)
                page.locator(".copy-response").first.click()
                assert "E = mc^2" in page.evaluate("navigator.clipboard.readText()")

                if output:
                    page.screenshot(
                        path=str(output / "stream-math.png"), animations="disabled"
                    )
                assert not errors, errors
                context.close()
            finally:
                _stop(server, thread)

        browser.close()
    print(
        "PASS: reload reattaches to the in-flight turn and replays partial text; "
        "retry notices stay in one entry and update their attempt count; "
        "inline and display LaTeX render locally; every assistant turn has a copy tag"
    )


if __name__ == "__main__":
    main()
