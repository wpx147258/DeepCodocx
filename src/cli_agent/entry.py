"""Click entry point: REPL by default, ``run`` for one-shot output."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import click

from . import __version__
from .assembly import BuildOptions, RuntimeBundle, build_runtime
from .cli.repl import RichAsker, run_print, run_repl
from .errors import CliAgentError


def _common_options(f: Any) -> Any:
    f = click.option(
        "--provider", default=None, help="Provider preset (explicit; no inference)."
    )(f)
    f = click.option("--model", default=None, help="Model id.")(f)
    f = click.option(
        "--permission-mode",
        type=click.Choice(["bypass", "accept-edits", "default"]),
        default=None,
        help="Permission mode.",
    )(f)
    f = click.option(
        "--config",
        "config_path",
        type=click.Path(dir_okay=False, path_type=Path),
        default=None,
        help="Explicit TOML config path.",
    )(f)
    f = click.option(
        "--max-turns", type=int, default=None, help="Maximum model calls per turn."
    )(f)
    f = click.option(
        "--thinking/--no-thinking",
        "show_thinking",
        default=None,
        help=(
            "Show model reasoning (display only; use --extended-thinking to request "
            "it). Default: on in the REPL, off for run."
        ),
    )(f)
    f = click.option(
        "--extended-thinking/--no-extended-thinking",
        "thinking_enabled",
        default=None,
        help=(
            "Ask the provider for extended thinking (requires provider and model "
            "support, e.g. DeepSeek deepseek-flash)."
        ),
    )(f)
    f = click.option(
        "--thinking-budget",
        type=int,
        default=None,
        help=(
            "Token budget for extended thinking (default 4096; must be below "
            "max output)."
        ),
    )(f)
    f = click.option(
        "--no-memory", is_flag=True, help="Disable memory: no catalog in the prompt and no Remember tool."
    )(f)
    f = click.option(
        "-c",
        "--resume",
        "resume_id",
        metavar="[SESSION_ID]",
        flag_value="latest",
        default=None,
        is_flag=False,
        help="Resume a session; without an id, resume the latest for this project.",
    )(f)
    f = click.option("--verbose", is_flag=True, help="Verbose diagnostics on stderr.")(f)
    return f


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _build_options(
    common: dict[str, Any],
    *,
    persist: bool,
    memory_enabled: bool | None,
    asker: Any,
) -> BuildOptions:
    return BuildOptions(
        provider=common["provider"],
        model=common["model"],
        permission_mode=common["permission_mode"],
        resume_id=common["resume_id"],
        persist=persist,
        max_turns=common["max_turns"],
        memory_enabled=memory_enabled,
        thinking_enabled=common["thinking_enabled"],
        thinking_budget=common["thinking_budget"],
        asker=asker,
        config_path=common["config_path"],
    )


def _load(options: BuildOptions) -> RuntimeBundle:
    try:
        return build_runtime(options)
    except CliAgentError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(1) from exc


def _display_settings(
    common: dict[str, Any], *, default: bool
) -> tuple[bool, bool]:
    value = common["show_thinking"]
    if value is None:
        return default, False
    return bool(value), True


def _warn_display_only(bundle: RuntimeBundle, show: bool, explicit: bool) -> None:
    if explicit and show and getattr(bundle.runtime, "thinking", None) is None:
        click.echo(
            "note: --thinking only controls display; add --extended-thinking "
            "and use a reasoning-capable model to request thinking.",
            err=True,
        )


def _run_repl(common: dict[str, Any]) -> int:
    _configure_logging(common["verbose"])
    options = _build_options(
        common,
        persist=True,
        memory_enabled=False if common["no_memory"] else None,
        asker=RichAsker(),
    )
    bundle = _load(options)
    show, explicit = _display_settings(common, default=True)
    _warn_display_only(bundle, show, explicit)
    return asyncio.run(run_repl(bundle, show_reasoning=show))


def _run_one_shot(common: dict[str, Any], prompt: str, output_format: str) -> int:
    _configure_logging(common["verbose"])
    options = _build_options(
        common,
        persist=common["resume_id"] is not None,
        memory_enabled=False,
        asker=None,
    )
    bundle = _load(options)
    show, explicit = _display_settings(common, default=False)
    _warn_display_only(bundle, show, explicit)
    return asyncio.run(
        run_print(
            bundle,
            prompt,
            show_reasoning=show,
            output_format=output_format,
        )
    )


@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@_common_options
@click.version_option(version=__version__, prog_name="cli-agent")
@click.pass_context
def main(ctx: click.Context, **common: Any) -> None:
    """Start the interactive REPL. Use ``run`` for one-shot output."""
    ctx.obj = common
    if ctx.invoked_subcommand is not None:
        return
    raise SystemExit(_run_repl(common))


@main.command("run")
@_common_options
@click.option(
    "--output-format",
    type=click.Choice(["text", "json", "jsonl"], case_sensitive=False),
    default="text",
    show_default=True,
    help=(
        "One-shot output: plain text, one JSON result object, or JSONL events. "
        "stdout carries only data; diagnostics go to stderr."
    ),
)
@click.argument("prompt", required=False)
@click.pass_context
def run(ctx: click.Context, **common: Any) -> None:
    """Run one prompt and exit. Use ``-`` to read the prompt from stdin."""
    group = ctx.obj if isinstance(ctx.obj, dict) else {}
    for key, value in group.items():
        if key in ("verbose", "no_memory"):
            common[key] = bool(common.get(key)) or bool(value)
        elif common.get(key) is None:
            common[key] = value

    output_format = common.pop("output_format")
    prompt = common.pop("prompt")
    if prompt is None:
        raise click.UsageError("run requires a prompt (use '-' to read stdin)")
    if prompt == "-":
        prompt = sys.stdin.read()
    if not prompt.strip():
        raise click.UsageError("empty prompt")
    raise SystemExit(_run_one_shot(common, prompt, output_format))
