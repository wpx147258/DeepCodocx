"""REPL and one-shot hosts. They own input and rendering, never runtime state."""

from __future__ import annotations

import asyncio
import contextlib
import json

from rich.console import Console
from rich.markup import escape

from ..assembly import RuntimeBundle
from ..errors import DcxError
from ..permissions.policy import Answer, PermissionRequest
from ..runtime import AgentRuntime
from .commands import CommandDispatcher
from .input import read_user_input
from .render import JsonRenderer, PrintRenderer, Renderer


class RichAsker:
    """Interactive permission asker for the REPL."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    async def ask(self, request: PermissionRequest) -> Answer:
        preview = json.dumps(dict(request.tool_input), ensure_ascii=False)
        if len(preview) > 300:
            preview = preview[:300] + "..."
        self.console.print(
            f"\n[yellow]Permission required:[/yellow] {escape(request.tool_name)}"
        )
        self.console.print(f"[dim]{escape(request.reason)}[/dim]")
        self.console.print(f"[dim]{escape(preview)}[/dim]")
        try:
            answer = input("Allow? (y/n/a=always): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "no"
        if answer in ("a", "always"):
            return "always"
        return "yes" if answer in ("y", "yes") else "no"


async def run_repl(bundle: RuntimeBundle, *, show_reasoning: bool = True) -> int:
    renderer = Renderer(show_reasoning=show_reasoning)
    dispatcher = CommandDispatcher()
    runtime = bundle.runtime
    renderer.welcome(
        model=bundle.profile.model,
        provider=bundle.profile.provider_name,
        session_id=bundle.session_id,
        permission_mode=runtime.permission_mode,
    )
    forked_from = getattr(bundle, "forked_from", None)
    if forked_from:
        renderer.notice(f"Forked from session {forked_from}.")
    if bundle.degraded:
        renderer.notice("The resumed session had corrupt records; some were skipped.")
    if bundle.resumed:
        if runtime.messages:
            renderer.history(runtime.messages)
        else:
            renderer.notice("Resumed an empty session.")

    while True:
        try:
            text = read_user_input()
        except KeyboardInterrupt:
            renderer.notice("\nInterrupted. Press Ctrl+D to exit.")
            continue
        if text is None:
            break
        text = text.strip()
        if not text:
            continue
        if text.startswith("/"):
            result = await dispatcher.dispatch(text, runtime, renderer)
            if result.exit_repl:
                break
            continue

        task = asyncio.create_task(_consume_turn(runtime, text, renderer))
        try:
            await task
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            current = asyncio.current_task()
            if current is not None and hasattr(current, "uncancel"):
                current.uncancel()
            renderer.notice("Cancelled.")
        except DcxError as exc:
            renderer.error(str(exc))
        except Exception as exc:  # noqa: BLE001 - keep the REPL alive
            renderer.error(f"{type(exc).__name__}: {exc}")

    await runtime.close()
    return 0


async def _consume_turn(
    runtime: AgentRuntime,
    text: str,
    renderer: Renderer,
) -> None:
    async for event in runtime.run_turn(text):
        renderer.render(event)


async def run_print(
    bundle: RuntimeBundle,
    prompt: str,
    *,
    show_reasoning: bool = False,
    output_format: str = "text",
) -> int:
    if output_format == "text":
        renderer: PrintRenderer | JsonRenderer = PrintRenderer(
            show_reasoning=show_reasoning
        )
    else:
        renderer = JsonRenderer(
            session_id=bundle.session_id,
            provider=bundle.profile.provider_name,
            model=bundle.profile.model,
            stream=output_format == "jsonl",
            show_reasoning=show_reasoning,
            resumed=bundle.resumed,
        )

    forked_from = getattr(bundle, "forked_from", None)
    if forked_from and output_format == "text":
        import sys

        print(
            f"forked session {bundle.session_id} from {forked_from}",
            file=sys.stderr,
        )
    renderer.start()
    try:
        async for event in bundle.runtime.run_turn(prompt):
            renderer.render(event)
    except KeyboardInterrupt:
        renderer.interrupt()
        return 130
    except Exception as exc:  # noqa: BLE001 - one-shot mode reports and exits
        renderer.fatal(exc)
        return 2
    else:
        renderer.finish()
        complete = renderer.stop_reason == "end_turn" and not renderer.failed
        return 0 if complete else 2
    finally:
        await bundle.runtime.close()
