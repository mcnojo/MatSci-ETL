"""Build + write report shapes. Markdown rendering lives in `renderer.py`.

Three entry points:
- build_batch_report(client, batch_id, region) — bounded by BatchRunWorkflow.
- build_live_report(client, window, region) — rolling window of standalone runs.
- build_comparison_report(batch_report, live_report) — combine two prebuilt reports.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from temporalio.client import Client

from shared.s3_io import put_bytes

from ..batch.planner import batch_workflow_id
from .hardware import (
    BATCH_NAMESPACE,
    LIVE_NAMESPACE,
    fetch_gpu_stats_async,
    fetch_hardware_stats_async,
)
from .models import (
    ActivityStats,
    BatchReport,
    ComparisonReport,
    GpuStats,
    LiveReport,
    LiveWindow,
    WorkerInstanceStats,
    WorkflowStats,
)
from .renderer import (
    render_batch_markdown,
    render_comparison_markdown,
    render_live_markdown,
)
from .temporal_walker import (
    aggregate_activities,
    aggregate_workflows,
    batch_window,
    walk_batch,
    walk_live_window,
)


# --- batch ------------------------------------------------------------------


async def build_batch_report(
    *,
    client: Client,
    batch_id: str,
    region: str,
    pull_hardware: bool = True,
) -> BatchReport:
    """`pull_hardware=False` skips CloudWatch for local dev runs."""
    wf_id = batch_workflow_id(batch_id)
    records = await walk_batch(client, wf_id)

    start, close, status = batch_window(records, wf_id)
    if start is None:
        raise RuntimeError(f"BatchRunWorkflow {wf_id} has no start_time")
    window_end = close or datetime.now(timezone.utc)
    duration = (window_end - start).total_seconds() if close else None

    workflows = aggregate_workflows(records)
    activities = aggregate_activities(records)
    items_total, items_succeeded, items_failed = _items_from_pdf_workflows(workflows)

    hardware, period, gpu = await _pull_hardware_and_gpu(
        region, start, window_end, namespace=BATCH_NAMESPACE, enabled=pull_hardware,
    )

    return BatchReport(
        batch_id=batch_id,
        status=status.name if status is not None else "UNKNOWN",
        started_at=start,
        closed_at=close,
        duration_seconds=duration,
        items_total=items_total,
        items_succeeded=items_succeeded,
        items_failed=items_failed,
        workflows=workflows,
        activities=activities,
        hardware=hardware,
        gpu=gpu,
        flags=build_flags(workflows, activities, hardware),
        cloudwatch_period_seconds=period,
    )


def write_batch_report(report: BatchReport, report_root: str) -> dict[str, str]:
    """Write JSON + Markdown under `{report_root}/batches/{batch_id}/report/`."""
    base = f"{report_root.rstrip('/')}/batches/{report.batch_id}/report"
    return _write_pair(base, report.model_dump_json(indent=2), render_batch_markdown(report))


# --- live -------------------------------------------------------------------


async def build_live_report(
    *,
    client: Client,
    window: LiveWindow,
    region: str,
    pull_hardware: bool = True,
) -> LiveReport:
    records = await walk_live_window(client, window.since, window.until)

    workflows = aggregate_workflows(records)
    activities = aggregate_activities(records)
    # For live, every walked record is a top-level ProcessPdfWorkflow run —
    # so item count == workflow count for that type.
    items_total, items_succeeded, items_failed = _items_from_pdf_workflows(workflows)

    hardware, period, gpu = await _pull_hardware_and_gpu(
        region, window.since, window.until, namespace=LIVE_NAMESPACE, enabled=pull_hardware,
    )

    return LiveReport(
        window=window,
        items_total=items_total,
        items_succeeded=items_succeeded,
        items_failed=items_failed,
        workflows=workflows,
        activities=activities,
        hardware=hardware,
        gpu=gpu,
        flags=build_flags(workflows, activities, hardware),
        cloudwatch_period_seconds=period,
    )


def write_live_report(report: LiveReport, report_root: str) -> dict[str, str]:
    """Write JSON + Markdown under `{report_root}/live/reports/<window_iso>/`."""
    base = f"{report_root.rstrip('/')}/live/reports/{_window_slug(report.window)}"
    return _write_pair(base, report.model_dump_json(indent=2), render_live_markdown(report))


# --- comparison -------------------------------------------------------------


def build_comparison_report(batch: BatchReport, live: LiveReport) -> ComparisonReport:
    """Pure combinator over two prebuilt reports — no IO."""
    return ComparisonReport(
        generated_at=datetime.now(timezone.utc),
        batch=batch,
        live=live,
    )


def write_comparison_report(report: ComparisonReport, report_root: str) -> dict[str, str]:
    """Write JSON + Markdown under `{report_root}/comparisons/<utc_iso>/`."""
    slug = report.generated_at.strftime("%Y%m%dT%H%M%SZ")
    base = f"{report_root.rstrip('/')}/comparisons/{slug}"
    return _write_pair(base, report.model_dump_json(indent=2), render_comparison_markdown(report))


# --- shared helpers ---------------------------------------------------------


async def _pull_hardware_and_gpu(
    region: str, start: datetime, end: datetime,
    *, namespace: str, enabled: bool,
) -> tuple[list[WorkerInstanceStats], int, list[GpuStats]]:
    if not enabled:
        return [], 0, []
    async with asyncio.TaskGroup() as tg:
        worker_task = tg.create_task(
            fetch_hardware_stats_async(region, start, end, namespace=namespace)
        )
        gpu_task = tg.create_task(fetch_gpu_stats_async(region, start, end))
    hardware, period = worker_task.result()
    gpu = gpu_task.result()
    return hardware, period, gpu


def _items_from_pdf_workflows(workflows: list[WorkflowStats]) -> tuple[int, int, int]:
    # ProcessPdfWorkflow may be absent if a batch failed before fan-out, or
    # if a live window had no traffic.
    for w in workflows:
        if w.workflow_type == "ProcessPdfWorkflow":
            return w.count, w.success_count, w.failure_count + w.other_count
    return 0, 0, 0


def build_flags(
    workflows: list[WorkflowStats],
    activities: list[ActivityStats],
    hardware: list[WorkerInstanceStats],
) -> list[str]:
    """Surface anomalies so the report opens with the bad news."""
    flags: list[str] = []

    for a in activities:
        if a.failure_count > 0:
            flags.append(f"Activity {a.activity_type}: {a.failure_count}/{a.count} terminal failures")
        if a.retry_count > 0:
            flags.append(f"Activity {a.activity_type}: {a.retry_count} total retries across {a.count} invocations")
        s = a.start_to_close_seconds
        if s and s.p50 > 0 and s.p99 / s.p50 > 5:
            flags.append(
                f"Activity {a.activity_type}: p99={s.p99:.1f}s vs p50={s.p50:.1f}s (>5x — long tail)"
            )

    for w in workflows:
        if w.workflow_type == "ProcessPdfWorkflow" and w.failure_count > 0:
            flags.append(f"{w.failure_count}/{w.count} ProcessPdfWorkflow runs failed")

    for h in hardware:
        if h.cpu_active_percent and h.cpu_active_percent.p95 > 95:
            flags.append(f"Instance {h.instance_id}: CPU p95={h.cpu_active_percent.p95:.0f}% — saturated")
        if h.memory_used_percent and h.memory_used_percent.max > 90:
            flags.append(f"Instance {h.instance_id}: memory max={h.memory_used_percent.max:.0f}% — near limit")

    return flags


def _write_pair(base: str, json_body: str, md_body: str) -> dict[str, str]:
    json_uri = f"{base}/report.json"
    md_uri = f"{base}/report.md"
    put_bytes(json_uri, json_body.encode("utf-8"), "application/json")
    put_bytes(md_uri, md_body.encode("utf-8"), "text/markdown")
    return {"report_json_uri": json_uri, "report_md_uri": md_uri}


def _window_slug(window: LiveWindow) -> str:
    return f"{window.since.strftime('%Y%m%dT%H%M%SZ')}_{window.until.strftime('%Y%m%dT%H%M%SZ')}"
