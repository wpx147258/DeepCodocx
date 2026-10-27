"""Typed slash commands. The REPL interprets the result; nothing here mutates state directly."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import CliAgentError
from ..events import AgentNotice
from ..runtime import AgentRuntime
from .render import Renderer


@dataclass(frozen=True, slots=True)
class CommandResult:
    handled: bool
    exit_repl: bool = False


_HELP = """Commands:
  /help              show this help
  /clear             clear the conversation
  /compact           compact the context now
  /model [NAME]      show or switch model; use provider:model to switch provider
  /auto [on|off]     toggle full-permission mode (no approval prompts)
  /cost              show cumulative token usage
  /exit              leave the REPL

Key flags: -p/--print, -c/--resume, --provider, --model, --permission-mode."""


class CommandDispatcher:
    async def dispatch(
        self,
        text: str,
        runtime: AgentRuntime,
        renderer: Renderer,
    ) -> CommandResult:
        if not text.startswith("/"):
            return CommandResult(False)
        name, _, arguments = text[1:].partition(" ")
        name = name.strip().lower()
        arguments = arguments.strip()

        if name in ("exit", "quit"):
            return CommandResult(True, exit_repl=True)
        if name == "help":
            renderer.notice(_HELP)
            return CommandResult(True)
        if name == "clear":
            await runtime.clear()
            renderer.notice("Conversation cleared.")
            return CommandResult(True)
        if name == "compact":
            async for event in runtime.compact():
                renderer.render(event)
            return CommandResult(True)
        if name == "model":
            await self._model(arguments, runtime, renderer)
            return CommandResult(True)
        if name == "auto":
            self._auto(arguments, runtime, renderer)
            return CommandResult(True)
        if name == "cost":
            usage = runtime.usage
            renderer.notice(
                "tokens: "
                f"in {usage.input_tokens} (uncached), out {usage.output_tokens}, "
                f"cache read {usage.cache_read_tokens}, "
                f"cache write {usage.cache_write_tokens}"
            )
            window = runtime.context_window
            used = runtime.context_tokens
            share = f"{used / window * 100:.1f}%" if window else "n/a"
            renderer.notice(f"context: ~{used} / {window} tokens ({share})")
            return CommandResult(True)

        renderer.notice(f"Unknown command /{name}. Type /help.")
        return CommandResult(True)

    async def _model(
        self,
        argument: str,
        runtime: AgentRuntime,
        renderer: Renderer,
    ) -> None:
        if not argument:
            renderer.notice(f"{runtime.provider_name}:{runtime.model}")
            return
        if ":" in argument:
            provider, model = argument.split(":", 1)
            provider = provider.strip()
            model = model.strip()
        else:
            provider, model = runtime.provider_name, argument
        if not model:
            renderer.error("model name is empty")
            return
        try:
            await runtime.switch_model(provider, model)
        except Exception as exc:  # noqa: BLE001 - user-facing command
            renderer.error(f"could not switch model: {type(exc).__name__}: {exc}")
            return
        renderer.notice(f"switched to {provider}:{model}")

    def _auto(self, argument: str, runtime: AgentRuntime, renderer: Renderer) -> None:
        choice = argument.strip().lower()
        try:
            if choice in ("", "toggle"):
                mode = runtime.toggle_auto()
            elif choice in ("on", "yes", "true"):
                mode = runtime.set_auto(True)
            elif choice in ("off", "no", "false"):
                mode = runtime.set_auto(False)
            else:
                renderer.error("usage: /auto [on|off]")
                return
        except CliAgentError as exc:
            renderer.error(str(exc))
            return
        if mode == "bypass":
            renderer.notice(
                "permission mode: bypass — every tool runs without asking; "
                "use /auto off to restore approvals"
            )
        else:
            renderer.notice(f"permission mode: {mode}")


__all__ = ["CommandDispatcher", "CommandResult", "AgentNotice"]
