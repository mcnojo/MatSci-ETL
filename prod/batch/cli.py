"""Operator CLI for the batch path.

Commands:
    submit  --manifest <s3-uri|path>    Parse manifest, plan shards, start BatchRunWorkflow.
    status  <batch_id>                  Show progress of an in-flight or completed batch.
    cancel  <batch_id>                  Request graceful cancellation.
    report  <batch_id>                  Print the final report S3 URIs.

The CLI is intentionally thin — it parses arguments and reads/writes workflow
state via the Temporal client. All authoritative state lives in the
BatchRunWorkflow event history.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy

from prod.shared_infra.task_queues import (
    BATCH_WORKFLOW_EXECUTION_TIMEOUT,
    CPU_TASK_QUEUE,
)
from shared.config_loader import load_pipeline_config
from shared.temporal_client import connect_temporal

from .artifacts import read_manifest
from .planner import DEFAULT_SHARD_SIZE, shard_manifest
from .workflows.batch_run import BatchRunWorkflow
from .workflows.models import BatchRunInput, BatchRunOutput

console = Console()


def _load_batch_config(config_path: str | None) -> dict:
    if not config_path:
        return {}
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}


def _batch_workflow_id(batch_id: str) -> str:
    return f"batch-{batch_id}"


@click.group()
@click.option(
    "--config",
    "config_path",
    default="prod/batch/config/batch_config.yaml",
    show_default=True,
    help="Batch config YAML.",
)
@click.option(
    "--pipeline-config",
    "pipeline_config_path",
    default="etl/config/pipeline_config.yaml",
    show_default=True,
    help="Pipeline config YAML (anchored, URL-resolved at CLI time).",
)
@click.option("--temporal-address", default="localhost:7233", show_default=True)
@click.option("--temporal-namespace", default="default", show_default=True)
@click.option("-v", "--verbose", is_flag=True, default=False)
@click.pass_context
def cli(
    ctx: click.Context,
    config_path: str,
    pipeline_config_path: str,
    temporal_address: str,
    temporal_namespace: str,
    verbose: bool,
) -> None:
    """Batch operations CLI."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpcore", "httpx", "temporalio.service"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    ctx.ensure_object(dict)
    ctx.obj["batch_cfg"] = _load_batch_config(config_path)
    ctx.obj["pipeline_config_path"] = pipeline_config_path
    ctx.obj["temporal_address"] = temporal_address
    ctx.obj["temporal_namespace"] = temporal_namespace


@cli.command()
@click.option(
    "--manifest",
    "manifest_uri",
    required=True,
    help="s3://... URI or local path to a manifest JSON file.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Parse the manifest and print the shard plan without submitting.",
)
@click.option(
    "--wait",
    is_flag=True,
    default=False,
    help="Block until the workflow completes and print the report URIs.",
)
@click.pass_context
def submit(ctx: click.Context, manifest_uri: str, dry_run: bool, wait: bool) -> None:
    """Parse a manifest, shard it, and start a BatchRunWorkflow."""
    asyncio.run(_submit(ctx.obj, manifest_uri, dry_run=dry_run, wait=wait))


async def _submit(ctx_obj: dict, manifest_uri: str, *, dry_run: bool, wait: bool) -> None:
    batch_cfg = ctx_obj["batch_cfg"]
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

    if dry_run:
        console.print("\n[yellow]dry-run: not submitting[/yellow]")
        return

    # Resolve the pipeline config at submit time so the workflow input
    # carries absolute paths and resolved URLs (vLLM endpoints, etc.).
    pipeline_config = load_pipeline_config(ctx_obj["pipeline_config_path"])

    report_root = batch_cfg.get("report", {}).get("s3_root")
    if not report_root:
        console.print("[red]batch_config.yaml: report.s3_root must be set[/red]")
        sys.exit(1)

    concurrency = batch_cfg.get("concurrency", {})
    workflow_id = _batch_workflow_id(manifest.batch_id)

    client = await connect_temporal(
        ctx_obj["temporal_address"], namespace=ctx_obj["temporal_namespace"],
    )

    input_obj = BatchRunInput(
        batch_id=manifest.batch_id,
        manifest_uri=manifest_uri,
        pipeline_config=pipeline_config,
        report_root=report_root,
        shard_size=shard_size,
        shards_in_flight=concurrency.get("shards_in_flight", 8),
        pdfs_per_shard_in_flight=concurrency.get("pdfs_per_shard_in_flight", 8),
    )

    handle = await client.start_workflow(
        BatchRunWorkflow.run,
        input_obj,
        id=workflow_id,
        task_queue=CPU_TASK_QUEUE,
        execution_timeout=BATCH_WORKFLOW_EXECUTION_TIMEOUT,
        # Operator-supplied batch_id should be unique per run. Reject
        # accidental re-submissions; the operator can use --terminate-running
        # in a future revision if they want to force a redo.
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )
    console.print(f"\n[green]Started[/green] {workflow_id}  ({handle.first_execution_run_id})")

    if not wait:
        console.print(
            f"  Track: python -m prod.batch.cli status {manifest.batch_id}"
        )
        return

    console.print("Waiting for completion...")
    result: BatchRunOutput = await handle.result()
    console.print("[green]Done.[/green]")
    console.print(
        f"  total={result.total_items}  success={result.success_count}  "
        f"failure={result.failure_count}"
    )
    for label, uri in result.report_uris.items():
        console.print(f"  {label}: {uri}")


@cli.command()
@click.argument("batch_id")
@click.pass_context
def status(ctx: click.Context, batch_id: str) -> None:
    """Show progress of an in-flight or completed batch."""
    asyncio.run(_status(ctx.obj, batch_id))


async def _status(ctx_obj: dict, batch_id: str) -> None:
    client = await connect_temporal(
        ctx_obj["temporal_address"], namespace=ctx_obj["temporal_namespace"],
    )
    handle = client.get_workflow_handle(_batch_workflow_id(batch_id))
    desc = await handle.describe()
    console.print(f"workflow_id: {desc.id}")
    console.print(f"run_id:      {desc.run_id}")
    console.print(f"status:      {desc.status.name if desc.status else 'UNKNOWN'}")
    console.print(f"started:     {desc.start_time}")
    if desc.close_time:
        console.print(f"closed:      {desc.close_time}")
    if desc.status == WorkflowExecutionStatus.COMPLETED:
        result: BatchRunOutput = await handle.result()
        console.print(
            f"\nresult: total={result.total_items}  "
            f"success={result.success_count}  failure={result.failure_count}"
        )
        for label, uri in result.report_uris.items():
            console.print(f"  {label}: {uri}")


@cli.command()
@click.argument("batch_id")
@click.pass_context
def cancel(ctx: click.Context, batch_id: str) -> None:
    """Request graceful cancellation of a running batch."""
    asyncio.run(_cancel(ctx.obj, batch_id))


async def _cancel(ctx_obj: dict, batch_id: str) -> None:
    client = await connect_temporal(
        ctx_obj["temporal_address"], namespace=ctx_obj["temporal_namespace"],
    )
    handle = client.get_workflow_handle(_batch_workflow_id(batch_id))
    await handle.cancel()
    console.print(f"[yellow]Cancel requested for {batch_id}[/yellow]")
    console.print(
        "  Children (shards + per-PDF workflows) will receive cancellation "
        "via Temporal's default propagation."
    )


@cli.command()
@click.argument("batch_id")
@click.pass_context
def report(ctx: click.Context, batch_id: str) -> None:
    """Print the final report S3 URIs for a completed batch."""
    asyncio.run(_report(ctx.obj, batch_id))


async def _report(ctx_obj: dict, batch_id: str) -> None:
    client = await connect_temporal(
        ctx_obj["temporal_address"], namespace=ctx_obj["temporal_namespace"],
    )
    handle = client.get_workflow_handle(_batch_workflow_id(batch_id))
    desc = await handle.describe()
    if desc.status != WorkflowExecutionStatus.COMPLETED:
        console.print(
            f"[yellow]batch {batch_id} is {desc.status.name if desc.status else 'UNKNOWN'} — "
            f"no report yet[/yellow]"
        )
        sys.exit(2)
    result: BatchRunOutput = await handle.result()
    console.print(json.dumps({
        "batch_id": result.batch_id,
        "total_items": result.total_items,
        "success_count": result.success_count,
        "failure_count": result.failure_count,
        "report_uris": result.report_uris,
    }, indent=2))


if __name__ == "__main__":
    cli()
