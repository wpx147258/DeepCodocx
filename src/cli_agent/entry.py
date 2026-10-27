"""Click entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click

from . import __version__
from .assembly import BuildOptions, build_runtime
from .cli.repl import RichAsker, run_print, run_repl
from .errors import CliAgentError


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-p",
    "--print",
    "print_prompt",
    metavar="PROMPT",
    default=None,
    help="One-shot mode. Use '-' to read the prompt from stdin.",
)
@click.option(
    "-c",
    "--resume",
    "resume_id",
    metavar="[SESSION_ID]",
    flag_value="latest",
    default=None,
    is_flag=False,
    help="Resume a session; without an id, resume the latest for this project.",
)
@click.option("--provider", default=None, help="Provider preset (explicit; no inference).")
@click.option("--model", default=None, help="Model id.")
@click.option(
    "--permission-mode",
    type=click.Choice(["bypass", "accept-edits", "default"]),
    default=None,
    help="Permission mode.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Explicit TOML config path.",
)
@click.option("--max-turns", type=int, default=None, help="Maximum model calls per turn.")
@click.option(
    "--thinking/--no-thinking",
    "show_thinking",
    default=None,
    help=(
        "Show model reasoning (display only; use --extended-thinking to request it). "
        "Default: on in the REPL, off in print mode; print mode writes to stderr."
    ),
)
@click.option(
    "--extended-thinking/--no-extended-thinking",
    "thinking_enabled",
    default=None,
    help=(
        "Ask the provider for extended thinking (requires provider and model "
        "support, e.g. DeepSeek deepseek-flash)."
    ),
)
@click.option(
    "--thinking-budget",
    type=int,
    default=None,
    help="Token budget for extended thinking (default 4096; must be below max output).",
)
@click.option("--no-memory", is_flag=True, help="Disable memory: no catalog in the prompt and no Remember tool.")
@click.option("--verbose", is_flag=True, help="Verbose diagnostics on stderr.")
@click.version_option(version=__version__, prog_name="cli-agent")
@click.argument("prompt", required=False)
def main(
    print_prompt: str | None,
    resume_id: str | None,
    provider: str | None,
    model: str | None,
    permission_mode: str | None,
    config_path: Path | None,
    max_turns: int | None,
    show_thinking: bool | None,
    thinking_enabled: bool | None,
    thinking_budget: int | None,
    no_memory: bool,
    verbose: bool,
    prompt: str | None,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if print_prompt is not None and prompt is not None:
        raise click.UsageError(
            "provide the prompt either with -p/--print or as an argument, not both"
        )
    if print_prompt is not None:
        user_prompt: str | None = print_prompt
        print_mode = True
    elif prompt is not None:
        user_prompt = prompt
        print_mode = True
    else:
        user_prompt = None
        print_mode = False

    if print_mode and user_prompt == "-":
        user_prompt = sys.stdin.read()
    if print_mode and not (user_prompt or "").strip():
        raise click.UsageError("empty prompt")

    persist = (not print_mode) or resume_id is not None
    memory_enabled = (
        False if print_mode else (False if no_memory else None)
    )
    options = BuildOptions(
        provider=provider,
        model=model,
        permission_mode=permission_mode,
        resume_id=resume_id,
        persist=persist,
        max_turns=max_turns,
        memory_enabled=memory_enabled,
        thinking_enabled=thinking_enabled,
        thinking_budget=thinking_budget,
        asker=None if print_mode else RichAsker(),
        config_path=config_path,
    )

    try:
        bundle = build_runtime(options)
    except CliAgentError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(1) from exc

    if show_thinking is None:
        show_thinking = not print_mode
        explicit_show_thinking = False
    else:
        explicit_show_thinking = True

    if (
        explicit_show_thinking
        and show_thinking
        and getattr(bundle.runtime, "thinking", None) is None
    ):
        click.echo(
            "note: --thinking only controls display; add --extended-thinking "
            "and use a reasoning-capable model to request thinking.",
            err=True,
        )

    if print_mode:
        assert user_prompt is not None
        code = asyncio.run(
            run_print(bundle, user_prompt, show_reasoning=show_thinking)
        )
    else:
        code = asyncio.run(run_repl(bundle, show_reasoning=show_thinking))
    raise SystemExit(code)
