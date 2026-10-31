"""Click entry point: REPL by default, ``run`` for one-shot output."""

from __future__ import annotations

import asyncio
import logging
import secrets
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import click

from . import __version__
from .assembly import BuildOptions, RuntimeBundle, build_runtime
from .cli import indexes as _indexes  # noqa: F401 - registers document index commands
from .cli.ingest import docs as docs_group
from .cli.repl import RichAsker, run_print, run_repl
from .errors import DcxError


def _common_options(f: Any) -> Any:
    f = click.option("--mode", type=click.Choice(["normal", "deepdocx"]), default=None, help="Session mode; DeepDocx uses the remembered or configured library by default.")(f)
    f = click.option("--library", type=click.Path(file_okay=False, path_type=Path), default=None, help="Document library directory (already converted basic/Marker bundles).")(f)
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
        "--max-turns",
        type=click.IntRange(min=1),
        default=None,
        help="Maximum model calls per turn (minimum 1).",
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
    f = click.option(
        "--fork",
        is_flag=True,
        help=(
            "Copy the resumed session under a new id and continue in the copy; "
            "the original stays untouched. Requires -c/--resume."
        ),
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
        fork=common["fork"],
        persist=persist,
        max_turns=common["max_turns"],
        memory_enabled=memory_enabled,
        thinking_enabled=common["thinking_enabled"],
        thinking_budget=common["thinking_budget"],
        asker=asker,
        config_path=common["config_path"],
        mode=common.get("mode"),
        library=str(common["library"]) if common.get("library") else None,
    )


def _load(options: BuildOptions) -> RuntimeBundle:
    try:
        return build_runtime(options)
    except DcxError as exc:
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


def _run_web(
    common: dict[str, Any],
    *,
    host: str,
    port: int,
    open_browser: bool,
    token: str | None = None,
) -> int:
    from .web.server import WebAsker, serve

    _configure_logging(common["verbose"])
    asker = WebAsker()
    options = _build_options(
        common,
        persist=True,
        memory_enabled=False if common["no_memory"] else None,
        asker=asker,
    )
    bundle = _load(options)

    def session_factory(session_id: str | None) -> RuntimeBundle:
        return build_runtime(replace(options, resume_id=session_id, fork=False, mode=None,
                                     library=None, session_profile=None))

    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "warning: the web UI is exposed beyond loopback; API access requires "
            "the printed token.",
            file=sys.stderr,
        )
        token = token or secrets.token_urlsafe(24)
    serve(
        bundle,
        asker,
        host=host,
        port=port,
        open_browser=open_browser,
        factory=session_factory,
        token=token,
    )
    return 0


def _merge_common(ctx: click.Context, common: dict[str, Any]) -> None:
    group = ctx.obj if isinstance(ctx.obj, dict) else {}
    for key, value in group.items():
        if key in ("verbose", "no_memory"):
            common[key] = bool(common.get(key)) or bool(value)
        elif common.get(key) is None:
            common[key] = value


@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@_common_options
@click.version_option(version=__version__, prog_name="dcx")
@click.pass_context
def main(ctx: click.Context, **common: Any) -> None:
    """Start the REPL. Use ``run`` for one-shot output or ``web`` for the browser."""
    ctx.obj = common
    if ctx.invoked_subcommand is not None:
        return
    raise SystemExit(_run_repl(common))


main.add_command(docs_group)


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
    _merge_common(ctx, common)

    output_format = common.pop("output_format")
    prompt = common.pop("prompt")
    if prompt is None:
        raise click.UsageError("run requires a prompt (use '-' to read stdin)")
    if prompt == "-":
        prompt = sys.stdin.read()
    if not prompt.strip():
        raise click.UsageError("empty prompt")
    raise SystemExit(_run_one_shot(common, prompt, output_format))


@main.command("web")
@_common_options
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address. The web UI has no authentication.",
)
@click.option(
    "--port",
    type=click.IntRange(1, 65_535),
    default=8765,
    show_default=True,
    help="Port for the local web client.",
)
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    show_default=True,
    help="Open the browser once the server is listening.",
)
@click.option(
    "--token",
    default=None,
    help=(
        "Require this token for API requests. Non-loopback hosts get a random "
        "token automatically."
    ),
)
@click.pass_context
def web(
    ctx: click.Context,
    host: str,
    port: int,
    open_browser: bool,
    token: str | None,
    **common: Any,
) -> None:
    """Serve the local chat client in a browser."""
    _merge_common(ctx, common)
    raise SystemExit(
        _run_web(
            common,
            host=host,
            port=port,
            open_browser=open_browser,
            token=token,
        )
    )
