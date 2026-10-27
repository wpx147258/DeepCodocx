"""Rich event rendering for the REPL and plain output for print mode."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.text import Text

from ..events import (
    AgentContextEvent,
    AgentError,
    AgentEvent,
    AgentNotice,
    AgentRetry,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnFinished,
    AgentUsage,
)
from ..messages import Message, TextBlock, ToolResultBlock, ToolUseBlock, Usage

# Stop reasons that mean the turn did not produce a trustworthy answer.
_FAILED_STOP_REASONS = frozenset({"refusal", "error", "max_turns"})


class Renderer:
    def __init__(
        self,
        console: Console | None = None,
        *,
        show_reasoning: bool = True,
    ) -> None:
        self.console = console or Console(highlight=False)
        self.error_console = Console(stderr=True, highlight=False)
        self.last_usage = Usage()
        self.show_reasoning = show_reasoning
        self._streamed = False
        self._reasoning_open = False

    def welcome(
        self,
        *,
        model: str,
        provider: str,
        session_id: str,
        permission_mode: str = "default",
    ) -> None:
        self.console.print(
            f"[bold]cli-agent[/bold] · {escape(provider)}/{escape(model)} · "
            f"session {escape(session_id)}"
        )
        if permission_mode == "bypass":
            mode = "[yellow]mode: bypass — no approval prompts[/yellow]"
        else:
            mode = f"[dim]mode: {escape(permission_mode)}[/dim]"
        self.console.print(
            f"{mode} [dim]· Type /help for commands, Ctrl+D to exit.[/dim]\n"
        )

    def history(self, messages: Sequence[Message], *, limit: int = 20) -> None:
        """Render the tail of a resumed conversation."""
        if not messages:
            return
        hidden = max(0, len(messages) - limit)
        self._break_line()
        self.console.print("[dim]── resumed history ──[/dim]")
        if hidden:
            self.console.print(f"[dim]... {hidden} earlier message(s) hidden ...[/dim]")
        for message in messages[-limit:]:
            role = "you" if message.role == "user" else "assistant"
            lines: list[str] = []
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    lines.append(escape(_clip(block.text, 400)))
                elif isinstance(block, ToolUseBlock):
                    lines.append(
                        f"[dim]› {escape(block.name)} "
                        f"{escape(_short_json(block.input, 120))}[/dim]"
                    )
                elif isinstance(block, ToolResultBlock):
                    text = escape(_clip(_short_content(block.content, 200), 200))
                    style = "red" if block.is_error else "dim"
                    lines.append(f"[{style}]← {text}[/{style}]")
            if not lines:
                continue
            self.console.print(f"[bold]{role}[/bold] " + "\n  ".join(lines))
        self.console.print("[dim]── end of history ──[/dim]")

    def render(self, event: AgentEvent) -> None:
        if isinstance(event, AgentTextDelta):
            if event.reasoning:
                if not self.show_reasoning or not event.text:
                    return
                if not self._reasoning_open:
                    self._break_line()
                    self.console.print("[dim]thinking...[/dim]")
                    self._reasoning_open = True
                self.console.print(
                    Text(event.text), end="", style="dim italic", highlight=False
                )
                self._streamed = True
                return
            self._close_reasoning()
            self.console.print(Text(event.text), end="", highlight=False)
            self._streamed = True
        elif isinstance(event, AgentToolStarted):
            self._break_line()
            preview = _short_json(event.input)
            self.console.print(
                f"[cyan]> {escape(event.name)}[/cyan] [dim]{escape(preview)}[/dim]"
            )
        elif isinstance(event, AgentToolFinished):
            self._break_line()
            text = _short_content(event.content)
            if event.is_error:
                self.console.print(f"[red]  error:[/red] {escape(text)}")
            else:
                self.console.print(f"[dim]  {escape(text)}[/dim]")
        elif isinstance(event, AgentRetry):
            self._break_line()
            note = f"retrying {event.attempt}/{event.max_attempts}"
            if event.partial:
                note += " — the text above belongs to the abandoned attempt"
            self.console.print(
                f"[yellow]{note}[/yellow] [dim]{escape(event.reason)}[/dim]"
            )
        elif isinstance(event, AgentUsage):
            self.last_usage = event.total
        elif isinstance(event, AgentContextEvent):
            self._break_line()
            detail = f" {event.detail}" if event.detail else ""
            self.console.print(f"[dim]\\[{event.kind}]{escape(detail)}[/dim]")
        elif isinstance(event, AgentNotice):
            self._break_line()
            self.console.print(f"[dim]{escape(event.text)}[/dim]")
        elif isinstance(event, AgentError):
            self._break_line()
            self.error_console.print(
                f"[red]error[/red] ({escape(event.kind)}): {escape(event.message)}"
            )
        elif isinstance(event, AgentTurnFinished):
            self._break_line()
            stop = "" if event.stop_reason == "end_turn" else f" · stopped: {event.stop_reason}"
            self.console.print(
                f"[dim]tokens: in {event.total.input_tokens} / "
                f"out {event.total.output_tokens}{escape(stop)}[/dim]"
            )

    def notice(self, text: str) -> None:
        self._break_line()
        self.console.print(f"[dim]{escape(text)}[/dim]")

    def error(self, text: str) -> None:
        self._break_line()
        self.error_console.print(f"[red]error:[/red] {escape(text)}")

    def _close_reasoning(self) -> None:
        if self._reasoning_open:
            self.console.print()
            self._reasoning_open = False
            self._streamed = False

    def _break_line(self) -> None:
        self._close_reasoning()
        if self._streamed:
            self.console.print()
            self._streamed = False


class PrintRenderer:
    """Print mode: stdout carries assistant text only.

    When reasoning is enabled it is written to stderr so pipelines still receive
    only the final answer on stdout.
    """

    def __init__(self, *, show_reasoning: bool = False) -> None:
        self.show_reasoning = show_reasoning
        self.failed = False
        self._streamed = False
        self._reasoning_open = False

    def render(self, event: AgentEvent) -> None:
        if isinstance(event, AgentTextDelta):
            if event.reasoning:
                if not self.show_reasoning or not event.text:
                    return
                if not self._reasoning_open:
                    sys.stderr.write("\n[thinking] ")
                    self._reasoning_open = True
                sys.stderr.write(event.text)
                sys.stderr.flush()
                return
            self._close_reasoning()
            sys.stdout.write(event.text)
            sys.stdout.flush()
            self._streamed = True
        elif isinstance(event, AgentRetry):
            self._close_reasoning()
            note = f"\n[retrying {event.attempt}/{event.max_attempts}: {event.reason}]"
            if event.partial:
                note += " (earlier partial output is stale)"
            sys.stderr.write(note + "\n")
            sys.stderr.flush()
        elif isinstance(event, (AgentToolStarted, AgentToolFinished)):
            self._close_reasoning()
        elif isinstance(event, AgentError):
            self.failed = True
            self._close_reasoning()
            print(f"error ({event.kind}): {event.message}", file=sys.stderr)
        elif isinstance(event, AgentTurnFinished):
            self._close_reasoning()
            if self._streamed:
                sys.stdout.write("\n")
                sys.stdout.flush()
            if event.stop_reason in _FAILED_STOP_REASONS:
                self.failed = True
                print(f"stopped: {event.stop_reason}", file=sys.stderr)

    def _close_reasoning(self) -> None:
        if self._reasoning_open:
            sys.stderr.write("\n")
            sys.stderr.flush()
            self._reasoning_open = False


def _short_json(value: Any, limit: int = 160) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _clip(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"


def _short_content(content: tuple[Any, ...], limit: int = 240) -> str:
    text = "".join(getattr(block, "text", "") for block in content)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."
