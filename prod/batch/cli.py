"""Operator CLI for the batch path.

Phase C: the workflow owns the batch lifecycle (scale up, await pollers,
fan out, write report, scale down). The CLI is now a thin entry point —
`submit` is the primary command, kept for power users / debug runs (the
Phase D Lambda is the production trigger).

Commands:
  submit            Start a BatchRunWorkflow. Default waits for completion.
  status            Describe an in-flight or completed batch.
  cancel            Request graceful cancellation (workflow's finally
                    block tears the fleet down).
  report            Re-run the rich report build (Temporal + CloudWatch).
                    Use after batch completion if the embedded build_report
                    activity didn't run (no-fleet mode) or failed.
  wait-for-workers  Diagnostic — block until activity pollers register on
                    the named task queues. Useful when debugging worker
                    bootstrap.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import click
import yaml
from rich.console import Console
from rich.table import Table
from temporalio.api.enums.v1 import TaskQueueKind, TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy

from prod.shared_infra.task_queues import (
    BATCH_WORKFLOW_EXECUTION_TIMEOUT,
    CPU_TASK_QUEUE,
    GPU_TASK_QUEUE,
)
from shared.config_loader import load_pipeline_config
from shared.temporal_client import connect_temporal

from .artifacts import read_manifest
from .planner import DEFAULT_SHARD_SIZE, batch_workflow_id, shard_manifest
from .reports import BatchReport, build_report, write_report
from .workflows.batch_run import BatchRunWorkflow
from .workflows.models import BatchRunInput, BatchRunOutput

console = Console()

_QUEUE_NAMES = {"cpu": CPU_TASK_QUEUE, "gpu": GPU_TASK_QUEUE}


# --- group ------------------------------------------------------------------


@click.group()
@click.option("--config", "config_path",
              default="prod/batch/config/batch_config.yaml", show_default=True)
@click.option("--pipeline-config", "pipeline_config_path",
              default="etl/config/pipeline_config.yaml", show_default=True)
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
    ctx.obj["batch_cfg"] = _load_yaml(config_path)
    ctx.obj["pipeline_config_path"] = pipeline_config_path
    ctx.obj["temporal_address"] = temporal_address
    ctx.obj["temporal_namespace"] = temporal_namespace


# --- helpers ----------------------------------------------------------------


def _load_yaml(path: str | None) -> dict:
    if not path:
        return {}
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _fleet_kwargs(batch_cfg: dict, *, manage_fleet: bool) -> dict:
    """Pull the fleet block out of batch_config.yaml into BatchRunInput kwargs.

    `manage_fleet=False` returns an empty dict (workflow skips lifecycle) —
    used by the debug `submit --no-manage-fleet` path.
    """
    if not manage_fleet:
        return {}
    fleet = batch_cfg.get("fleet") or {}
    required = ("region", "cpu_queue_asg_name", "gpu_queue_asg_name",
                "cpu_queue_max_workers", "gpu_queue_max_workers")
    missing = [k for k in required if not fleet.get(k)]
    if missing:
        raise click.BadParameter(
            f"batch_config.yaml: fleet block is missing required keys: {missing}. "
            f"Pass --no-manage-fleet to run against a pre-existing fleet."
        )
    return {
        "region": fleet["region"],
        "cpu_queue_asg_name": fleet["cpu_queue_asg_name"],
        "gpu_queue_asg_name": fleet["gpu_queue_asg_name"],
        "cpu_queue_desired": fleet["cpu_queue_max_workers"],
        "gpu_queue_desired": fleet["gpu_queue_max_workers"],
        "worker_registration_timeout_s": fleet.get("worker_registration_timeout_s", 600),
    }


async def _has_activity_pollers(client: Client, namespace: str, task_queue: str) -> bool:
    resp = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=namespace,
            task_queue=TaskQueue(name=task_queue, kind=TaskQueueKind.TASK_QUEUE_KIND_NORMAL),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        ),
    )
    return len(resp.pollers) >= 1


async def _await_pollers(
    client: Client, namespace: str,
    queues: list[str], timeout_s: int, poll_s: float,
) -> None:
    targets = set(queues)
    deadline = time.monotonic() + timeout_s
    ready: set[str] = set()
    console.print(f"Waiting for pollers on {sorted(targets)} (timeout {timeout_s}s)...")
    while time.monotonic() < deadline:
        pending = sorted(targets - ready)
        if not pending:
            break
        results = await asyncio.gather(
            *(_has_activity_pollers(client, namespace, q) for q in pending),
        )
        for q, ok in zip(pending, results):
            if ok:
                console.print(f"  [green]{q}[/green] ready")
                ready.add(q)
        if ready == targets:
            return
        await asyncio.sleep(poll_s)
    raise click.ClickException(f"timeout: no pollers on {sorted(targets - ready)}")


def _print_plan(manifest, shard_size: int) -> None:
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


async def _start_workflow(
    ctx_obj: dict, client: Client, manifest, manifest_uri: str,
    *, manage_fleet: bool,
) -> tuple[object, str]:
    batch_cfg = ctx_obj["batch_cfg"]
    report_root = batch_cfg.get("report", {}).get("s3_root")
    if not report_root:
        raise click.BadParameter("batch_config.yaml: report.s3_root must be set")

    concurrency = batch_cfg.get("concurrency", {})
    shard_size = batch_cfg.get("planner", {}).get("shard_size", DEFAULT_SHARD_SIZE)
    pipeline_config = load_pipeline_config(ctx_obj["pipeline_config_path"])
    workflow_id = batch_workflow_id(manifest.batch_id)

    handle = await client.start_workflow(
        BatchRunWorkflow.run,
        BatchRunInput(
            batch_id=manifest.batch_id,
            manifest_uri=manifest_uri,
            pipeline_config=pipeline_config,
            report_root=report_root,
            shard_size=shard_size,
            shards_in_flight=concurrency.get("shards_in_flight", 8),
            pdfs_per_shard_in_flight=concurrency.get("pdfs_per_shard_in_flight", 8),
            **_fleet_kwargs(batch_cfg, manage_fleet=manage_fleet),
        ),
        id=workflow_id,
        task_queue=CPU_TASK_QUEUE,
        execution_timeout=BATCH_WORKFLOW_EXECUTION_TIMEOUT,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )
    return handle, workflow_id


def _print_result(result: BatchRunOutput) -> None:
    console.print(
        f"  total={result.total_items}  success={result.success_count}  "
        f"failure={result.failure_count}"
    )
    for label, uri in result.report_uris.items():
        console.print(f"  {label}: {uri}")


async def _build_report_and_write(
    client: Client, batch_id: str, region: str, out_dir: str,
    *, skip_hardware: bool = False,
) -> None:
    console.print(f"Building report for batch [bold]{batch_id}[/bold]")
    rpt = await build_report(
        client=client, batch_id=batch_id, region=region,
        pull_hardware=not skip_hardware,
    )
    uris = write_report(rpt, out_dir)
    _print_report_summary(rpt, uris)


def _print_report_summary(r: BatchReport, uris: dict[str, str]) -> None:
    console.print(f"  status:     {r.status}")
    console.print(
        f"  items:      {r.items_total} "
        f"(ok={r.items_succeeded} fail={r.items_failed})"
    )
    console.print(f"  workflows:  {sum(w.count for w in r.workflows)}")
    console.print(f"  activities: {sum(a.count for a in r.activities)}")
    console.print(f"  instances:  {len(r.hardware)}")
    if r.flags:
        console.print(f"\n[yellow]Flags ({len(r.flags)}):[/yellow]")
        for f in r.flags:
            console.print(f"  • {f}")
    for label, uri in uris.items():
        console.print(f"  {label}: {uri}")


# --- commands ---------------------------------------------------------------


@cli.command()
@click.option("--manifest", "manifest_uri", required=True,
              help="s3://... URI or local path to a manifest JSON file.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Print the shard plan without submitting.")
@click.option("--wait/--no-wait", default=True, show_default=True,
              help="Block until the workflow completes (default).")
@click.option("--manage-fleet/--no-manage-fleet", default=True, show_default=True,
              help="When set, the workflow scales the ASGs up/down. Pass "
                   "--no-manage-fleet to run against a pre-existing fleet "
                   "(local dev / debug).")
@click.pass_context
def submit(
    ctx: click.Context, manifest_uri: str,
    dry_run: bool, wait: bool, manage_fleet: bool,
) -> None:
    """Start a BatchRunWorkflow.

    The workflow scales the fleet, awaits pollers, fans out shards, writes
    reports, and tears the fleet down — all without the CLI staying
    connected past the start call.
    """
    asyncio.run(_submit(
        ctx.obj, manifest_uri,
        dry_run=dry_run, wait=wait, manage_fleet=manage_fleet,
    ))


async def _submit(
    ctx_obj: dict, manifest_uri: str, *,
    dry_run: bool, wait: bool, manage_fleet: bool,
) -> None:
    batch_cfg = ctx_obj["batch_cfg"]
    manifest = read_manifest(manifest_uri)
    shard_size = batch_cfg.get("planner", {}).get("shard_size", DEFAULT_SHARD_SIZE)
    _print_plan(manifest, shard_size)
    if manage_fleet:
        console.print(
            "[bold]Fleet management[/bold]: workflow will scale the ASGs from batch_config.yaml's "
            "fleet block; cancellation triggers an ABANDON-detached teardown."
        )
    else:
        console.print("[yellow]--no-manage-fleet[/yellow]: workflow runs against pre-existing pollers; "
                      "operator owns the fleet.")

    if dry_run:
        console.print("\n[yellow]dry-run: not submitting[/yellow]")
        return

    client = await connect_temporal(
        ctx_obj["temporal_address"], namespace=ctx_obj["temporal_namespace"],
    )
    handle, workflow_id = await _start_workflow(
        ctx_obj, client, manifest, manifest_uri, manage_fleet=manage_fleet,
    )
    console.print(f"\n[green]Started[/green] {workflow_id}  ({handle.first_execution_run_id})")

    if not wait:
        console.print(f"  Track: python -m prod.batch.cli status {manifest.batch_id}")
        return

    console.print("Waiting for completion (Ctrl-C cancels — finally block tears the fleet down)...")
    try:
        _print_result(await handle.result())
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelling workflow...[/yellow]")
        await handle.cancel()
        console.print("Cancellation sent. Workflow's finally block will scale the fleet to zero.")
        sys.exit(130)


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
    handle = client.get_workflow_handle(batch_workflow_id(batch_id))
    desc = await handle.describe()
    console.print(f"workflow_id: {desc.id}")
    console.print(f"run_id:      {desc.run_id}")
    console.print(f"status:      {desc.status.name if desc.status else 'UNKNOWN'}")
    console.print(f"started:     {desc.start_time}")
    if desc.close_time:
        console.print(f"closed:      {desc.close_time}")
    if desc.status == WorkflowExecutionStatus.COMPLETED:
        console.print("")
        _print_result(await handle.result())


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
    await client.get_workflow_handle(batch_workflow_id(batch_id)).cancel()
    console.print(f"[yellow]Cancel requested for {batch_id}[/yellow]")


@cli.command()
@click.argument("batch_id")
@click.option("--region", default=None,
              help="AWS region for CloudWatch. Defaults to fleet.region.")
@click.option("--skip-hardware", is_flag=True, default=False,
              help="Skip the CloudWatch fetch (local dev).")
@click.option("--out", "out_dir", default=None,
              help="Override report root. Defaults to report.s3_root.")
@click.pass_context
def report(
    ctx: click.Context, batch_id: str,
    region: Optional[str], skip_hardware: bool, out_dir: Optional[str],
) -> None:
    """Walk a batch's Temporal histories + CW metrics into a report."""
    asyncio.run(_report(ctx.obj, batch_id, region, skip_hardware, out_dir))


async def _report(
    ctx_obj: dict, batch_id: str,
    region: Optional[str], skip_hardware: bool, out_dir: Optional[str],
) -> None:
    batch_cfg = ctx_obj["batch_cfg"]
    resolved_region = region or batch_cfg.get("fleet", {}).get("region") or "us-west-2"
    resolved_out = out_dir or batch_cfg.get("report", {}).get("s3_root")
    if not resolved_out:
        raise click.BadParameter("no report root — pass --out or set report.s3_root")
    if not skip_hardware and not resolved_region:
        raise click.BadParameter("no region — pass --region or set fleet.region")

    client = await connect_temporal(
        ctx_obj["temporal_address"], namespace=ctx_obj["temporal_namespace"],
    )
    await _build_report_and_write(
        client, batch_id, resolved_region, resolved_out, skip_hardware=skip_hardware,
    )


@cli.command("wait-for-workers")
@click.option("--queues", "queues_str", default="cpu,gpu", show_default=True,
              help="Comma-separated queues to wait on.")
@click.option("--timeout", "timeout_s", default=600, show_default=True, type=int)
@click.option("--poll-interval", "poll_s", default=5.0, show_default=True, type=float)
@click.pass_context
def wait_for_workers(
    ctx: click.Context, queues_str: str, timeout_s: int, poll_s: float,
) -> None:
    """Diagnostic — block until activity pollers exist on each named queue."""
    asyncio.run(_wait_for_workers(ctx.obj, queues_str, timeout_s, poll_s))


async def _wait_for_workers(
    ctx_obj: dict, queues_str: str, timeout_s: int, poll_s: float,
) -> None:
    requested = {q.strip().lower() for q in queues_str.split(",") if q.strip()}
    invalid = requested - _QUEUE_NAMES.keys()
    if invalid:
        raise click.BadParameter(
            f"unknown queues: {sorted(invalid)} (valid: {sorted(_QUEUE_NAMES)})",
        )
    if not requested:
        raise click.BadParameter("--queues must include at least one of: cpu, gpu")
    targets = [_QUEUE_NAMES[q] for q in requested]
    client = await connect_temporal(
        ctx_obj["temporal_address"], namespace=ctx_obj["temporal_namespace"],
    )
    await _await_pollers(client, ctx_obj["temporal_namespace"], targets, timeout_s, poll_s)
    console.print("[green]All queues ready.[/green]")


if __name__ == "__main__":
    cli()
