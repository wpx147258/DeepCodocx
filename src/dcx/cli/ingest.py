"""Administrative document-ingestion commands (never an agent tool)."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ..config import load_config
from ..deepdocx.ingest import IngestService
from ..deepdocx.library import DocumentLibrary


@click.group("docs")
def docs() -> None:
    """Build and inspect document bundles (system-side ingestion)."""


@docs.command("ingest")
@click.option("--library", "library_path", type=click.Path(path_type=Path), required=True)
@click.option("--mode", type=click.Choice(["basic", "marker", "both"]), default="basic")
@click.option("--force", is_flag=True, help="Replace an existing rendition.")
@click.option("--config", "config_path", type=click.Path(dir_okay=False, path_type=Path))
@click.argument("sources", nargs=-1, type=click.Path(path_type=Path, exists=True))
def ingest(
    library_path: Path,
    mode: str,
    force: bool,
    config_path: Path | None,
    sources: tuple[Path, ...],
) -> None:
    """Upload local PDFs into a library and build bundles."""
    if not sources:
        raise click.UsageError("Provide at least one PDF file")
    config = load_config(config_path)
    library = DocumentLibrary(library_path)
    if not library.root.is_dir():
        raise click.ClickException("Document library directory is unavailable")
    service = IngestService(config.paths, config.ingest)
    job = None
    try:
        job = service.create_job(library.id, library.root, mode, force=force)
        for source in sources:
            service.add_file(job["id"], source.name, source)
        service.start(job["id"])
        result = service.wait(job["id"])
    except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
        if job is not None:
            service.cancel(job["id"])
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        if job is not None:
            service.cancel(job["id"])
        raise
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.get("status") == "succeeded" else 1)


@docs.command("jobs")
@click.option("--config", "config_path", type=click.Path(dir_okay=False, path_type=Path))
def jobs(config_path: Path | None) -> None:
    """List recent ingestion jobs."""
    config = load_config(config_path)
    service = IngestService(config.paths, config.ingest)
    click.echo(json.dumps(service.list()[:50], ensure_ascii=False, indent=2))


@docs.command("cancel")
@click.argument("job_id")
@click.option("--config", "config_path", type=click.Path(dir_okay=False, path_type=Path))
def cancel(job_id: str, config_path: Path | None) -> None:
    """Cancel a running ingestion job."""
    config = load_config(config_path)
    service = IngestService(config.paths, config.ingest)
    try:
        result = service.cancel(job_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@docs.command("list")
@click.option("--library", "library_path", type=click.Path(path_type=Path), required=True)
@click.option("--config", "config_path", type=click.Path(dir_okay=False, path_type=Path))
def list_docs(library_path: Path, config_path: Path | None) -> None:
    """List documents and available renditions in a library."""
    load_config(config_path)
    library = DocumentLibrary(library_path)
    click.echo(json.dumps(library.documents(), ensure_ascii=False, indent=2))
