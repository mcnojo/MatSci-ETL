"""Operator CLI: `python -m prod.reports {batch,live,compare} ...`."""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import click
import yaml
from rich.console import Console

from shared.s3_io import get_bytes
from shared.temporal.client import connect_temporal
from shared.temporal.operator_address import resolve_operator_address

from .builder import (
    build_batch_report,
    build_comparison_report,
    build_live_report,
    write_batch_report,
    write_comparison_report,
    write_live_report,
)
from .models import BatchReport, LiveReport, LiveWindow

console = Console()

_DURATION_RE = re.compile(r"^(?P<n>\d+)(?P<u>[smhd])$")


def _parse_duration(s: str) -> timedelta:
    m = _DURATION_RE.match(s.strip().lower())
    if not m:
        raise click.BadParameter(f"duration must look like 30m, 24h, 7d, 90s — got {s!r}")
    n = int(m.group("n"))
    return {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }[m.group("u")]


def _load_batch_cfg(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@click.group()
@click.option("--temporal-address", default=None,
              help="Temporal gRPC endpoint. Resolution order: flag -> "
                   "TEMPORAL_ADDRESS env -> shared/temporal terraform output "
                   "cpu_pipeline_public_ip:7233 -> localhost:7233.")
@click.option("--temporal-namespace", default="default", show_default=True)
@click.option("--batch-config", default="prod/batch/config/batch_config.yaml",
              show_default=True,
              help="Used to resolve the default report root.")
@click.option("--region", "region_default", default="us-west-2", show_default=True,
              help="Default AWS region for CloudWatch lookups; per-command --region overrides.")
@click.pass_context
def cli(
    ctx: click.Context,
    temporal_address: str | None,
    temporal_namespace: str,
    batch_config: str,
    region_default: str,
) -> None:
    """Build batch / live / comparison reports."""
    ctx.ensure_object(dict)
    resolved_address, source = resolve_operator_address(temporal_address)
    if source != "flag":
        console.print(f"[dim]Temporal address: {resolved_address} (from {source})[/dim]")
    ctx.obj["temporal_address"] = resolved_address
    ctx.obj["temporal_namespace"] = temporal_namespace
    ctx.obj["batch_config"] = batch_config
    batch_cfg = _load_batch_cfg(batch_config) if Path(batch_config).exists() else {}
    ctx.obj["report_root_default"] = batch_cfg.get("report", {}).get("s3_root")
    ctx.obj["region_default"] = region_default


@cli.command()
@click.argument("batch_id")
@click.option("--region", default=None,
              help="AWS region for CloudWatch. Defaults to the group --region (us-west-2).")
@click.option("--out", "out_dir", default=None,
              help="Override report root. Defaults to batch_config report.s3_root.")
@click.option("--skip-hardware", is_flag=True, default=False,
              help="Skip the CloudWatch fetch (local dev).")
@click.pass_context
def batch(
    ctx: click.Context, batch_id: str,
    region: Optional[str], out_dir: Optional[str], skip_hardware: bool,
) -> None:
    """Build a report for one BatchRunWorkflow."""
    asyncio.run(_run_batch(ctx.obj, batch_id, region, out_dir, skip_hardware))


async def _run_batch(
    ctx_obj: dict, batch_id: str,
    region: Optional[str], out_dir: Optional[str], skip_hardware: bool,
) -> None:
    resolved_region = region or ctx_obj["region_default"]
    resolved_out = out_dir or ctx_obj["report_root_default"]
    if not resolved_out:
        raise click.BadParameter("no report root — pass --out or set batch_config.report.s3_root")

    client = await connect_temporal(
        ctx_obj["temporal_address"], namespace=ctx_obj["temporal_namespace"],
    )
    console.print(f"Building batch report [bold]{batch_id}[/bold] in [cyan]{resolved_region}[/cyan]")
    report = await build_batch_report(
        client=client, batch_id=batch_id, region=resolved_region,
        pull_hardware=not skip_hardware,
    )
    uris = write_batch_report(report, resolved_out)
    _print_summary(
        f"batch={report.batch_id} status={report.status}",
        f"items={report.items_total} ok={report.items_succeeded} fail={report.items_failed}",
        report.flags, uris,
    )


@cli.command()
@click.option("--since", "since_str", required=True,
              help="Rolling-window start as a duration before --until (e.g. 24h, 90m).")
@click.option("--until", "until_str", default=None,
              help="Rolling-window end as a duration before now (e.g. 0s, 1h). "
                   "Default: 0s (now).")
@click.option("--region", default=None,
              help="AWS region for CloudWatch.")
@click.option("--out", "out_dir", default=None,
              help="Override report root. Defaults to batch_config report.s3_root.")
@click.option("--skip-hardware", is_flag=True, default=False)
@click.pass_context
def live(
    ctx: click.Context, since_str: str, until_str: Optional[str],
    region: Optional[str], out_dir: Optional[str], skip_hardware: bool,
) -> None:
    """Build a rolling-window report of standalone ProcessPdfWorkflow runs."""
    now = datetime.now(timezone.utc)
    until = now - _parse_duration(until_str) if until_str else now
    since = until - _parse_duration(since_str)
    window = LiveWindow(since=since, until=until)
    asyncio.run(_run_live(ctx.obj, window, region, out_dir, skip_hardware))


async def _run_live(
    ctx_obj: dict, window: LiveWindow,
    region: Optional[str], out_dir: Optional[str], skip_hardware: bool,
) -> None:
    resolved_region = region or ctx_obj["region_default"]
    resolved_out = out_dir or ctx_obj["report_root_default"]
    if not resolved_out:
        raise click.BadParameter("no report root — pass --out or set batch_config.report.s3_root")

    client = await connect_temporal(
        ctx_obj["temporal_address"], namespace=ctx_obj["temporal_namespace"],
    )
    console.print(
        f"Building live report [{window.since.isoformat()} -> {window.until.isoformat()}] "
        f"in [cyan]{resolved_region}[/cyan]"
    )
    report = await build_live_report(
        client=client, window=window, region=resolved_region,
        pull_hardware=not skip_hardware,
    )
    uris = write_live_report(report, resolved_out)
    _print_summary(
        f"window={window.since.isoformat()} -> {window.until.isoformat()}",
        f"items={report.items_total} ok={report.items_succeeded} fail={report.items_failed}",
        report.flags, uris,
    )


@cli.command()
@click.option("--batch", "batch_id", required=True,
              help="The batch_id to compare against (must already have a report on S3).")
@click.option("--live-window", "live_window_str", default="24h", show_default=True,
              help="Rolling window for the live side (e.g. 24h).")
@click.option("--region", default=None, help="AWS region for CloudWatch.")
@click.option("--out", "out_dir", default=None,
              help="Override report root. Defaults to batch_config report.s3_root.")
@click.option("--skip-hardware", is_flag=True, default=False)
@click.pass_context
def compare(
    ctx: click.Context, batch_id: str, live_window_str: str,
    region: Optional[str], out_dir: Optional[str], skip_hardware: bool,
) -> None:
    """Build a side-by-side Live | Batch comparison report."""
    asyncio.run(_run_compare(
        ctx.obj, batch_id, _parse_duration(live_window_str),
        region, out_dir, skip_hardware,
    ))


async def _run_compare(
    ctx_obj: dict, batch_id: str, live_delta: timedelta,
    region: Optional[str], out_dir: Optional[str], skip_hardware: bool,
) -> None:
    resolved_region = region or ctx_obj["region_default"]
    resolved_out = out_dir or ctx_obj["report_root_default"]
    if not resolved_out:
        raise click.BadParameter("no report root — pass --out or set batch_config.report.s3_root")

    # write_batch_report() round-trips, so the workflow's end-of-run report
    # serves as a cache here — rebuild only when absent.
    cached = _try_load_batch_report(resolved_out, batch_id)
    client = await connect_temporal(
        ctx_obj["temporal_address"], namespace=ctx_obj["temporal_namespace"],
    )
    if cached is not None:
        console.print(f"Loaded cached batch report from S3 for [bold]{batch_id}[/bold]")
        batch_report = cached
    else:
        console.print(f"No cached batch report; building from Temporal + CW for [bold]{batch_id}[/bold]")
        batch_report = await build_batch_report(
            client=client, batch_id=batch_id, region=resolved_region,
            pull_hardware=not skip_hardware,
        )

    now = datetime.now(timezone.utc)
    window = LiveWindow(since=now - live_delta, until=now)
    console.print(
        f"Building live side for window {window.since.isoformat()} -> {window.until.isoformat()}"
    )
    live_report = await build_live_report(
        client=client, window=window, region=resolved_region,
        pull_hardware=not skip_hardware,
    )

    comparison = build_comparison_report(batch_report, live_report)
    uris = write_comparison_report(comparison, resolved_out)
    _print_summary(
        f"compare batch={batch_id} vs live_window={_live_window_human(live_delta)}",
        f"batch_items={batch_report.items_total} live_items={live_report.items_total}",
        flags=[], uris=uris,
    )


def _try_load_batch_report(report_root: str, batch_id: str) -> Optional[BatchReport]:
    uri = f"{report_root.rstrip('/')}/batches/{batch_id}/report/report.json"
    try:
        raw = get_bytes(uri).decode("utf-8")
    except Exception:                         # missing object — fall through to rebuild
        return None
    try:
        return BatchReport.model_validate_json(raw)
    except Exception:
        # Older / future schema mismatch: prefer rebuild over half-truth.
        return None


def _live_window_human(d: timedelta) -> str:
    s = int(d.total_seconds())
    if s % 86400 == 0:
        return f"{s // 86400}d"
    if s % 3600 == 0:
        return f"{s // 3600}h"
    if s % 60 == 0:
        return f"{s // 60}m"
    return f"{s}s"


def _print_summary(
    header: str, line: str, flags: list[str], uris: dict[str, str],
) -> None:
    console.print(f"[green]✓[/green] {header}")
    console.print(f"  {line}")
    if flags:
        console.print(f"[yellow]Flags ({len(flags)}):[/yellow]")
        for f in flags:
            console.print(f"  • {f}")
    for label, uri in uris.items():
        console.print(f"  {label}: {uri}")


if __name__ == "__main__":
    cli()
