"""Administrative index commands; no provider or runtime required."""

from __future__ import annotations

import json
import time
from pathlib import Path

import click

from ..config import load_config
from ..deepdocx.library import DocumentLibrary
from ..deepdocx.semantic.service import TERMINAL, IndexService
from .ingest import docs


@docs.group("index")
def indexes():
    """Inspect, synchronize and query derived multimodal indexes."""


def options(function):
    function = click.option(
        "--library", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True
    )(function)
    return click.option(
        "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
    )(function)


def services(library, config_path):
    config = load_config(config_path)
    return IndexService(config.paths.data_dir, config.index, config.caption), DocumentLibrary(
        library
    )


@indexes.command("status")
@options
def status(library, config_path):
    service, source = services(library, config_path)
    click.echo(json.dumps(service.status(source), ensure_ascii=False, indent=2))


@indexes.command("sync")
@options
@click.option(
    "--captions/--no-captions", default=None, help="Generate independent visual descriptions."
)
@click.option(
    "--force", is_flag=True, help="Regenerate derived representations, including cached inference."
)
@click.option("--wait/--no-wait", default=True)
def sync(library, config_path, captions, force, wait):
    service, source = services(library, config_path)
    job = None
    try:
        job = service.start(source, generate=captions, force=force)
        while wait:
            jobs = service.status(source)["jobs"]
            job = next(j for j in jobs if j["id"] == job["id"])
            if job["status"] in TERMINAL:
                break
            time.sleep(0.5)
        click.echo(json.dumps(job, ensure_ascii=False, indent=2))
        if wait and job["status"] != "succeeded":
            raise SystemExit(1)
    except KeyboardInterrupt:
        if job:
            service.cancel(source, job["id"])
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@indexes.command("cancel")
@options
@click.argument("job_id")
def cancel(library, config_path, job_id):
    service, source = services(library, config_path)
    try:
        click.echo(json.dumps(service.cancel(source, job_id), ensure_ascii=False, indent=2))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@indexes.command("search")
@options
@click.argument("text", default="")
@click.option("--image", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--representation", multiple=True)
def search(library, config_path, text, image, representation):
    service, source = services(library, config_path)
    try:
        click.echo(
            json.dumps(
                service.engine(source).search(
                    text, image=image, representations=list(representation)
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
