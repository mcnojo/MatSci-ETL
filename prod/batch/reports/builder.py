"""Orchestrate: walk Temporal, pull CloudWatch, render JSON + Markdown."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from temporalio.client import Client, WorkflowExecutionStatus

from ..artifacts import put_artifact_bytes
from ..planner import batch_workflow_id
from .hardware import fetch_hardware_stats_async
from .models import (
    ActivityStats,
    BatchReport,
    StatSummary,
    WorkerInstanceStats,
    WorkflowStats,
)
from .temporal_walker import (
    aggregate_activities,
    aggregate_workflows,
    batch_window,
    walk_batch,
)


async def build_report(
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

    hardware: list[WorkerInstanceStats] = []
    period = 0
    if pull_hardware:
        hardware, period = await fetch_hardware_stats_async(region, start, window_end)

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
        flags=_build_flags(workflows, activities, hardware),
        cloudwatch_period_seconds=period,
    )


def write_report(report: BatchReport, report_root: str) -> dict[str, str]:
    """Write JSON + Markdown under `{report_root}/{batch_id}/report/`."""
    base = f"{report_root.rstrip('/')}/{report.batch_id}/report"
    json_uri = f"{base}/report.json"
    md_uri = f"{base}/report.md"
    put_artifact_bytes(json_uri, report.model_dump_json(indent=2).encode("utf-8"), "application/json")
    put_artifact_bytes(md_uri, render_markdown(report).encode("utf-8"), "text/markdown")
    return {"report_json_uri": json_uri, "report_md_uri": md_uri}


def _items_from_pdf_workflows(workflows: list[WorkflowStats]) -> tuple[int, int, int]:
    # ProcessPdfWorkflow may be absent if the batch failed before fan-out.
    for w in workflows:
        if w.workflow_type == "ProcessPdfWorkflow":
            return w.count, w.success_count, w.failure_count + w.other_count
    return 0, 0, 0


def _build_flags(
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


# --- Markdown rendering -----------------------------------------------------


def render_markdown(report: BatchReport) -> str:
    lines: list[str] = []
    lines.append(f"# Batch report: {report.batch_id}")
    lines.append("")
    lines.append(f"- **Status:** {report.status}")
    lines.append(f"- **Started:** {report.started_at.isoformat()}")
    if report.closed_at:
        lines.append(f"- **Closed:**  {report.closed_at.isoformat()}")
    if report.duration_seconds is not None:
        lines.append(f"- **Duration:** {_fmt_duration(report.duration_seconds)}")
    lines.append(
        f"- **Items:** total={report.items_total} "
        f"succeeded={report.items_succeeded} failed={report.items_failed}"
    )
    lines.append(
        f"- **CloudWatch resolution:** {report.cloudwatch_period_seconds}s"
        if report.cloudwatch_period_seconds
        else "- **CloudWatch resolution:** n/a (hardware fetch skipped)"
    )

    if report.flags:
        lines.append("")
        lines.append("## Flags")
        for f in report.flags:
            lines.append(f"- {f}")

    lines.append("")
    lines.append("## Workflows")
    lines.append("")
    lines.append("| Type | Count | OK | Failed | Other | Duration p50 / p95 / max (s) |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for w in report.workflows:
        lines.append(
            f"| {w.workflow_type} | {w.count} | {w.success_count} | "
            f"{w.failure_count} | {w.other_count} | {_fmt_stat(w.duration_seconds)} |"
        )

    lines.append("")
    lines.append("## Activities")
    lines.append("")
    lines.append(
        "| Type | Count | OK | Failed | Retries | "
        "Start→Close p50 / p95 / max (s) | Sched→Close p50 / p95 / max (s) |"
    )
    lines.append("|---|---:|---:|---:|---:|---|---|")
    for a in report.activities:
        lines.append(
            f"| {a.activity_type} | {a.count} | {a.success_count} | "
            f"{a.failure_count} | {a.retry_count} | "
            f"{_fmt_stat(a.start_to_close_seconds)} | "
            f"{_fmt_stat(a.schedule_to_close_seconds)} |"
        )

    lines.append("")
    lines.append("## Hardware (per instance)")
    if not report.hardware:
        lines.append("")
        lines.append("_No hardware data — CloudWatch returned no metrics for the batch window._")
    else:
        lines.append("")
        for h in report.hardware:
            lines.append(f"### {h.instance_id}")
            if h.first_seen_at and h.last_seen_at:
                lines.append(f"_Active: {h.first_seen_at.isoformat()} → {h.last_seen_at.isoformat()}_")
            lines.append("")
            lines.append("| Metric | p50 | p95 | max |")
            lines.append("|---|---:|---:|---:|")
            lines.append(f"| CPU active % | {_fmt_stat_short(h.cpu_active_percent)} |")
            lines.append(f"| Memory used % | {_fmt_stat_short(h.memory_used_percent)} |")
            lines.append(f"| Worker CPU % | {_fmt_stat_short(h.worker_cpu_percent)} |")
            lines.append(f"| Worker RSS (MiB) | {_fmt_stat_short(_bytes_to_mib(h.worker_memory_rss_bytes))} |")
            lines.append(f"| Net sent (KiB/s) | {_fmt_stat_short(_bytes_to_kib(h.net_bytes_sent_per_second))} |")
            lines.append(f"| Net recv (KiB/s) | {_fmt_stat_short(_bytes_to_kib(h.net_bytes_recv_per_second))} |")
            lines.append("")

    return "\n".join(lines) + "\n"


def _fmt_stat(s: Optional[StatSummary]) -> str:
    if s is None or s.count == 0:
        return "—"
    return f"{s.p50:.1f} / {s.p95:.1f} / {s.max:.1f}  (n={s.count})"


def _fmt_stat_short(s: Optional[StatSummary]) -> str:
    if s is None or s.count == 0:
        return "— | — | —"
    return f"{s.p50:.1f} | {s.p95:.1f} | {s.max:.1f}"


def _bytes_to_mib(s: Optional[StatSummary]) -> Optional[StatSummary]:
    return _scale(s, 1.0 / (1024 * 1024)) if s else None


def _bytes_to_kib(s: Optional[StatSummary]) -> Optional[StatSummary]:
    return _scale(s, 1.0 / 1024) if s else None


def _scale(s: StatSummary, k: float) -> StatSummary:
    return StatSummary(
        count=s.count, max=s.max * k, p50=s.p50 * k, p95=s.p95 * k, p99=s.p99 * k,
    )


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.2f}h"
