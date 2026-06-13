"""Operator CLI for the batch path. The workflow owns the lifecycle; this is a thin entry point.

Reports moved to `python -m prod.reports {batch,live,compare}`.
"""

import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
import click
import yaml
from rich.console import Console
from rich.table import Table
from temporalio.api.enums.v1 import TaskQueueKind, TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy

from shared.temporal.task_queues import (
    BATCH_WORKFLOW_EXECUTION_TIMEOUT,
    CPU_TASK_QUEUE,
    GPU_TASK_QUEUE,
)
from shared.config_loader import load_pipeline_config
from shared.temporal.client import connect_temporal

from .artifacts import read_manifest
from .planner import (
    DEFAULT_SHARD_SIZE,
    batch_workflow_id,
    manifest_uri as default_manifest_uri,
    shard_manifest,
)
from .workflows.batch_run import BatchRunWorkflow
from .workflows.models import BatchRunInput, BatchRunOutput

console = Console()

_QUEUE_NAMES = {"cpu": CPU_TASK_QUEUE, "gpu": GPU_TASK_QUEUE}

# Resolved relative to this file so the CLI works regardless of CWD.
_DEFAULT_BATCH_TF_DIR = str(Path(__file__).resolve().parent / "terraform")


@click.group()
@click.option("--config", "config_path",
              default="prod/batch/config/batch_config.yaml", show_default=True)
@click.option("--pipeline-config", "pipeline_config_path",
              default="etl/config/pipeline_config.yaml", show_default=True)
@click.option("--temporal-address", default="localhost:7233", show_default=True)
@click.option("--temporal-namespace", default="default", show_default=True)
@click.option("--terraform-dir", "terraform_dir",
              default=_DEFAULT_BATCH_TF_DIR, show_default=True,
              help="prod/batch terraform module dir; outputs are the source of truth "
                   "for ASG names + scale-up targets. Only consulted when managing the fleet.")
@click.option("-v", "--verbose", is_flag=True, default=False)
@click.pass_context
def cli(
    ctx: click.Context,
    config_path: str,
    pipeline_config_path: str,
    temporal_address: str,
    temporal_namespace: str,
    terraform_dir: str,
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
    ctx.obj["terraform_dir"] = terraform_dir


def _load_yaml(path: str | None) -> dict:
    if not path:
        return {}
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


# Fleet wiring is sourced from prod/batch terraform outputs — terraform owns
# the ASG names, scale-up target (max_size), and worker registration timeout.
_REQUIRED_FLEET_OUTPUTS = (
    "region", "cpu_queue_asg_name", "gpu_queue_asg_name",
    "cpu_queue_max_size", "gpu_queue_max_size",
    "worker_registration_timeout_s",
)
_ARTIFACT_BUCKET_OUTPUT = "artifact_bucket"


def _terraform_outputs(module_dir: str) -> dict:
    """Run `terraform output -json` on a module and return name→value.

    Raises ClickException with operator-actionable guidance for the three
    realistic failure modes: terraform missing, module not initialized, motif
    not up (outputs empty).
    """
    try:
        result = subprocess.run(
            ["terraform", f"-chdir={module_dir}", "output", "-json"],
            check=False, capture_output=True, text=True,
        )
    except FileNotFoundError as e:
        raise click.ClickException(
            f"`terraform` not found on PATH. The CLI reads fleet config from "
            f"terraform outputs at {module_dir}. Install terraform or pass "
            f"--no-manage-fleet to run against a pre-existing fleet."
        ) from e
    if result.returncode != 0:
        raise click.ClickException(
            f"`terraform output` failed in {module_dir} (exit {result.returncode}):\n"
            f"{(result.stderr or result.stdout).strip()}\n"
            f"Is the batch motif up? (bin/batch/up.sh)"
        )
    raw_json = result.stdout.strip() or "{}"
    raw = json.loads(raw_json)
    if not raw:
        raise click.ClickException(
            f"`terraform output` returned no outputs from {module_dir}. The "
            f"batch motif is down. Bring it up (bin/batch/up.sh) or pass "
            f"--no-manage-fleet to run against a pre-existing fleet."
        )
    return {k: v["value"] for k, v in raw.items()}


def _fleet_kwargs(*, manage_fleet: bool, terraform_dir: str) -> dict:
    # manage_fleet=False → empty dict; workflow skips lifecycle (debug path).
    if not manage_fleet:
        return {}
    outputs = _terraform_outputs(terraform_dir)
    missing = [k for k in _REQUIRED_FLEET_OUTPUTS if k not in outputs]
    if missing:
        raise click.ClickException(
            f"prod/batch terraform outputs are missing required keys: {missing}. "
            f"Re-apply the batch module (bin/tf.sh batch apply) and retry."
        )
    return {
        "region": outputs["region"],
        "cpu_queue_asg_name": outputs["cpu_queue_asg_name"],
        "gpu_queue_asg_name": outputs["gpu_queue_asg_name"],
        "cpu_queue_desired": outputs["cpu_queue_max_size"],
        "gpu_queue_desired": outputs["gpu_queue_max_size"],
        "worker_registration_timeout_s": outputs["worker_registration_timeout_s"],
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
            **_fleet_kwargs(
                manage_fleet=manage_fleet,
                terraform_dir=ctx_obj["terraform_dir"],
            ),
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


@cli.command()
@click.argument("batch_id")
@click.option("--manifest-uri", "manifest_uri_override", default=None,
              help="Override the derived manifest URI. Default: "
                   "s3://<artifact_bucket>/batches/incoming/<batch_id>/manifest.json "
                   "(bucket read from prod/batch terraform output).")
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
    ctx: click.Context, batch_id: str, manifest_uri_override: str | None,
    dry_run: bool, wait: bool, manage_fleet: bool,
) -> None:
    """Start a BatchRunWorkflow for an already-uploaded batch (workflow owns
    scale-up → fan-out → report → scale-down).

    BATCH_ID is the identifier printed by `bin/batch/submit.sh` after upload.
    """
    asyncio.run(_submit(
        ctx.obj, batch_id, manifest_uri_override,
        dry_run=dry_run, wait=wait, manage_fleet=manage_fleet,
    ))


def _resolve_manifest_uri(
    batch_id: str, override: str | None, terraform_dir: str,
) -> str:
    if override:
        return override
    outputs = _terraform_outputs(terraform_dir)
    bucket = outputs.get(_ARTIFACT_BUCKET_OUTPUT)
    if not bucket:
        raise click.ClickException(
            f"prod/batch terraform output {_ARTIFACT_BUCKET_OUTPUT!r} is "
            f"missing — cannot derive manifest URI. Pass --manifest-uri "
            f"s3://… to override, or bring the batch motif up "
            f"(bin/batch/up.sh)."
        )
    return default_manifest_uri(bucket, batch_id)


async def _submit(
    ctx_obj: dict, batch_id: str, manifest_uri_override: str | None, *,
    dry_run: bool, wait: bool, manage_fleet: bool,
) -> None:
    batch_cfg = ctx_obj["batch_cfg"]
    manifest_uri = _resolve_manifest_uri(
        batch_id, manifest_uri_override, ctx_obj["terraform_dir"],
    )
    console.print(f"Manifest URI: [cyan]{manifest_uri}[/cyan]")
    manifest = read_manifest(manifest_uri)
    if manifest.batch_id != batch_id:
        raise click.ClickException(
            f"batch_id mismatch — argument was {batch_id!r} but manifest "
            f"body has {manifest.batch_id!r}. Refusing to submit."
        )
    shard_size = batch_cfg.get("planner", {}).get("shard_size", DEFAULT_SHARD_SIZE)
    _print_plan(manifest, shard_size)
    if manage_fleet:
        console.print(
            "[bold]Fleet management[/bold]: workflow will scale the ASGs to their "
            "max_size (read from prod/batch terraform outputs); cancellation "
            "triggers an ABANDON-detached teardown."
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
