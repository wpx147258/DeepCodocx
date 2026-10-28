"""Typed slash commands. The REPL interprets the result; nothing here mutates state directly."""

from __future__ import annotations

from dataclasses import dataclass

from ..context.tokens import prompt_tokens
from ..errors import CliAgentError
from ..events import AgentNotice
from ..messages import Usage
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
  /memory [list]     list saved memories
  /memory forget X   delete the memory named X
  /cost              show cumulative token usage
  /exit              leave the REPL

Key flags: cli-agent run for one-shot, -c/--resume, --provider, --model, --permission-mode."""


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
            renderer.notice(
                "Compacting rewrites the prompt prefix; the next request re-reads "
                "the context once."
            )
            async for event in runtime.compact():
                renderer.render(event)
            return CommandResult(True)
        if name == "model":
            await self._model(arguments, runtime, renderer)
            return CommandResult(True)
        if name == "auto":
            self._auto(arguments, runtime, renderer)
            return CommandResult(True)
        if name == "memory":
            self._memory(arguments, runtime, renderer)
            return CommandResult(True)
        if name == "cost":
            usage = runtime.usage
            sent = prompt_tokens(usage)
            renderer.notice(
                f"tokens: prompt {sent} sent "
                f"(uncached {usage.input_tokens}, "
                f"cache read {usage.cache_read_tokens}, "
                f"cache write {usage.cache_write_tokens}), "
                f"out {usage.output_tokens}"
            )
            window = runtime.context_window
            used = runtime.context_tokens
            share = f"{used / window * 100:.1f}%" if window else "n/a"
            renderer.notice(f"context: ~{used} / {window} tokens ({share})")
            renderer.notice(
                "cache: "
                f"last {_cache_hit(runtime.last_usage)}, "
                f"session {_cache_hit(usage)}"
            )
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

    def _memory(self, argument: str, runtime: AgentRuntime, renderer: Renderer) -> None:
        action, _, rest = argument.strip().partition(" ")
        action = action.strip().lower()
        rest = rest.strip()
        if action in ("", "list"):
            records = runtime.memories()
            if not records:
                renderer.notice("No saved memories.")
                return
            lines = [
                f"- {record.name} ({record.type})"
                + (f": {record.description}" if record.description else "")
                for record in records
            ]
            renderer.notice("Saved memories:\n" + "\n".join(lines))
            return
        if action == "forget":
            if not rest:
                renderer.error("usage: /memory forget <name>")
                return
            if runtime.forget_memory(rest):
                renderer.notice(f"Forgot {rest!r}.")
            else:
                renderer.notice(f"No memory named {rest!r}.")
            return
        renderer.error("usage: /memory [list|forget <name>]")


def _cache_hit(usage: Usage) -> str:
    prompt = prompt_tokens(usage)
    if prompt <= 0:
        return "n/a"
    return f"{usage.cache_read_tokens / prompt * 100:.1f}% hit"


__all__ = ["CommandDispatcher", "CommandResult", "AgentNotice"]
