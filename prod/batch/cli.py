"""Operator CLI for the batch path.

Commands:
    submit  --manifest <s3-uri|path>    Parse manifest, plan shards, start workflow.
    status  <batch_id>                  Show progress of an in-flight or completed batch.
    cancel  <batch_id>                  Request graceful cancellation.
    report  <batch_id>                  Print the final report S3 URIs.

The CLI is intentionally thin — it parses arguments and reads workflow state
via the Temporal client. All authoritative state lives in the
BatchRunWorkflow event history.

Phase 3 (current): submit parses + shards the manifest and prints a dry-run
summary. Workflow starts come online in Phase 4.
"""

import asyncio
import logging
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from .artifacts import read_manifest
from .planner import DEFAULT_SHARD_SIZE, shard_manifest

console = Console()


def _load_batch_config(config_path: str | None) -> dict:
    if not config_path:
        return {}
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}


@click.group()
@click.option(
    "--config",
    "config_path",
    default="prod/batch/config/batch_config.yaml",
    show_default=True,
    help="Batch config YAML.",
)
@click.option("-v", "--verbose", is_flag=True, default=False)
@click.pass_context
def cli(ctx: click.Context, config_path: str, verbose: bool) -> None:
    """Batch operations CLI."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    ctx.ensure_object(dict)
    ctx.obj["config"] = _load_batch_config(config_path)


@cli.command()
@click.option(
    "--manifest",
    "manifest_uri",
    required=True,
    help="s3://... URI or local path to a manifest JSON file.",
)
@click.pass_context
def submit(ctx: click.Context, manifest_uri: str) -> None:
    """Parse a manifest, shard it, and (Phase 4+) start a BatchRunWorkflow."""
    asyncio.run(_submit(ctx.obj["config"], manifest_uri))


async def _submit(batch_cfg: dict, manifest_uri: str) -> None:
    manifest = read_manifest(manifest_uri)
    shard_size = batch_cfg.get("planner", {}).get("shard_size", DEFAULT_SHARD_SIZE)
    shards = shard_manifest(manifest, shard_size=shard_size)

    table = Table(title=f"Batch manifest: {manifest.batch_id}", show_header=True)
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Items", str(len(manifest.items)))
    table.add_row("Shards", str(len(shards)))
    table.add_row("Shard size", str(shard_size))
    table.add_row("Has config overrides", "yes" if manifest.config_overrides else "no")
    console.print(table)

    console.print("\n[bold]First 5 items:[/bold]")
    for item in manifest.items[:5]:
        console.print(f"  {item.document_id}  ->  {item.pdf_uri}")
    if len(manifest.items) > 5:
        console.print(f"  ... and {len(manifest.items) - 5} more")

    console.print(
        "\n[yellow]Phase 3 dry-run: workflow start is not yet wired. "
        "Phase 4 will submit BatchRunWorkflow here.[/yellow]"
    )


@cli.command()
@click.argument("batch_id")
def status(batch_id: str) -> None:
    """Show progress of an in-flight or completed batch."""
    _not_yet(batch_id, "status")


@cli.command()
@click.argument("batch_id")
def cancel(batch_id: str) -> None:
    """Request graceful cancellation of a running batch."""
    _not_yet(batch_id, "cancel")


@cli.command()
@click.argument("batch_id")
def report(batch_id: str) -> None:
    """Print the final report S3 URIs for a completed batch."""
    _not_yet(batch_id, "report")


def _not_yet(batch_id: str, action: str) -> None:
    console.print(
        f"[yellow]'{action}' is not yet wired (Phase 4 deliverable). "
        f"batch_id={batch_id}[/yellow]"
    )
    sys.exit(2)


if __name__ == "__main__":
    cli()
