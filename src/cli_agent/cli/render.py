"""Rich event rendering for the REPL and plain one-shot output."""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.text import Text

from ..events import (
    AgentContextEvent,
    AgentError,
    AgentEvent,
    AgentNotice,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnFinished,
    AgentUsage,
)
from ..messages import Usage


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

    def welcome(self, *, model: str, provider: str, session_id: str) -> None:
        self.console.print(
            f"[bold]cli-agent[/bold] · {escape(provider)}/{escape(model)} · "
            f"session {escape(session_id)}"
        )
        self.console.print("[dim]Type /help for commands, Ctrl+D to exit.[/dim]\n")

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
            self.console.print(
                f"[dim]tokens: in {event.total.input_tokens} / "
                f"out {event.total.output_tokens}[/dim]"
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
    """One-shot mode: stdout carries assistant text only.

    When reasoning is enabled it is written to stderr so pipelines still receive
    only the final answer on stdout. Diagnostics and notices also go to stderr.
    """

    def __init__(self, *, show_reasoning: bool = False) -> None:
        self.show_reasoning = show_reasoning
        self.failed = False
        self.stop_reason: str | None = None
        self._streamed = False
        self._reasoning_open = False

    def start(self) -> None:
        return None

    def finish(self) -> None:
        return None

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
        elif isinstance(event, (AgentToolStarted, AgentToolFinished)):
            self._close_reasoning()
        elif isinstance(event, AgentNotice):
            self._close_reasoning()
            print(event.text, file=sys.stderr)
        elif isinstance(event, AgentError):
            self.failed = True
            self._close_reasoning()
            print(f"error ({event.kind}): {event.message}", file=sys.stderr)
        elif isinstance(event, AgentTurnFinished):
            self.stop_reason = event.stop_reason
            self._close_reasoning()
            if self._streamed:
                sys.stdout.write("\n")
                sys.stdout.flush()

    def fatal(self, exc: Exception) -> None:
        self.failed = True
        self._close_reasoning()
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)

    def interrupt(self) -> None:
        self._close_reasoning()
        print("interrupted", file=sys.stderr)

    def _close_reasoning(self) -> None:
        if self._reasoning_open:
            sys.stderr.write("\n")
            sys.stderr.flush()
            self._reasoning_open = False


class JsonRenderer:
    """Machine-readable headless output.

    With ``stream=True`` each event is a JSON line (JSONL). With
    ``stream=False`` a single JSON object is written at ``finish``.
    stdout carries only JSON; diagnostics go to stderr.
    """

    def __init__(
        self,
        *,
        session_id: str,
        provider: str,
        model: str,
        stream: bool,
        show_reasoning: bool = False,
        resumed: bool = False,
    ) -> None:
        self.session_id = session_id
        self.provider = provider
        self.model = model
        self.stream = stream
        self.show_reasoning = show_reasoning
        self.resumed = resumed
        self.failed = False
        self.stop_reason: str | None = None
        self.total_usage = Usage()
        self.text_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.notices: list[str] = []
        self.context_events: list[dict[str, str]] = []
        self.error: dict[str, Any] | None = None
        self._started = False
        self._finished = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self.stream:
            self._write(
                {
                    "type": "session_start",
                    "session_id": self.session_id,
                    "provider": self.provider,
                    "model": self.model,
                    "resumed": self.resumed,
                }
            )

    def finish(self) -> None:
        if self._finished:
            return
        self.start()
        self._finished = True
        self._write(self._result())

    def render(self, event: AgentEvent) -> None:
        if isinstance(event, AgentTextDelta):
            if event.reasoning:
                if not self.show_reasoning:
                    return
                self.reasoning_parts.append(event.text)
                self._emit({"type": "reasoning", "text": event.text})
                return
            self.text_parts.append(event.text)
            self._emit({"type": "text", "text": event.text})
        elif isinstance(event, AgentToolStarted):
            record = {
                "tool_use_id": event.tool_use_id,
                "name": event.name,
                "input": dict(event.input),
                "is_error": None,
                "content": None,
            }
            self.tool_calls.append(record)
            self._emit(
                {
                    "type": "tool_started",
                    "tool_use_id": event.tool_use_id,
                    "name": event.name,
                    "input": dict(event.input),
                }
            )
        elif isinstance(event, AgentToolFinished):
            content = _content_text(event.content)
            for record in reversed(self.tool_calls):
                if record["tool_use_id"] == event.tool_use_id and record["content"] is None:
                    record["content"] = content
                    record["is_error"] = event.is_error
                    break
            self._emit(
                {
                    "type": "tool_finished",
                    "tool_use_id": event.tool_use_id,
                    "name": event.name,
                    "is_error": event.is_error,
                    "content": content,
                }
            )
        elif isinstance(event, AgentUsage):
            self.total_usage = event.total
            self._emit(
                {
                    "type": "usage",
                    "turn": _usage_dict(event.turn),
                    "total": _usage_dict(event.total),
                }
            )
        elif isinstance(event, AgentNotice):
            self.notices.append(event.text)
            self._emit({"type": "notice", "text": event.text})
        elif isinstance(event, AgentContextEvent):
            item = {"kind": event.kind, "detail": event.detail}
            self.context_events.append(item)
            self._emit({"type": "context", **item})
        elif isinstance(event, AgentError):
            self.failed = True
            self.error = {
                "message": event.message,
                "kind": event.kind,
                "status_code": event.status_code,
            }
            self._emit({"type": "error", **self.error})
        elif isinstance(event, AgentTurnFinished):
            self.stop_reason = event.stop_reason
            self.total_usage = event.total

    def fatal(self, exc: Exception) -> None:
        self.failed = True
        self.stop_reason = "error"
        self.error = {
            "message": f"{type(exc).__name__}: {exc}",
            "kind": "fatal",
            "status_code": None,
        }
        self._emit({"type": "error", **self.error})
        self.finish()

    def interrupt(self) -> None:
        self.failed = True
        self.stop_reason = "error"
        self.error = {
            "message": "interrupted",
            "kind": "interrupted",
            "status_code": None,
        }
        self._emit({"type": "error", **self.error})
        self.finish()

    def _result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "result",
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "resumed": self.resumed,
            "stop_reason": self.stop_reason,
            "is_error": self.failed,
            "is_complete": self.stop_reason == "end_turn",
            "text": "".join(self.text_parts),
            "tool_calls": self.tool_calls,
            "usage": _usage_dict(self.total_usage),
            "notices": self.notices,
            "context_events": self.context_events,
            "error": self.error,
        }
        if self.show_reasoning:
            result["reasoning"] = "".join(self.reasoning_parts)
        return result

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.stream:
            self._write(payload)

    def _write(self, payload: dict[str, Any]) -> None:
        sys.stdout.write(
            json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        )
        sys.stdout.flush()


def _short_json(value: Any, limit: int = 160) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _short_content(content: tuple[Any, ...], limit: int = 240) -> str:
    text = "".join(getattr(block, "text", "") for block in content)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _usage_dict(usage: Usage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }


def _content_text(content: tuple[Any, ...]) -> str:
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        parts.append(text if isinstance(text, str) else "[image]")
    return "".join(parts)
