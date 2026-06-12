"""Markdown rendering for batch, live, and comparison reports."""

from __future__ import annotations

from typing import Optional

from .models import (
    ActivityStats,
    BatchReport,
    ComparisonReport,
    GpuStats,
    LiveReport,
    StatSummary,
    WorkerInstanceStats,
    WorkflowStats,
)

def render_batch_markdown(report: BatchReport) -> str:
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
    lines.append(_cw_period_line(report.cloudwatch_period_seconds))
    _append_flags(lines, report.flags)
    _append_workflows(lines, report.workflows)
    _append_activities(lines, report.activities)
    _append_hardware(lines, report.hardware)
    _append_gpu(lines, report.gpu)
    return "\n".join(lines) + "\n"


def render_live_markdown(report: LiveReport) -> str:
    lines: list[str] = []
    lines.append(f"# Live report: {report.window.since.isoformat()} → {report.window.until.isoformat()}")
    lines.append("")
    lines.append(f"- **Window duration:** {_fmt_duration(report.window.duration_seconds)}")
    lines.append(
        f"- **Items:** total={report.items_total} "
        f"succeeded={report.items_succeeded} failed={report.items_failed}"
    )
    if report.window.duration_seconds > 0:
        throughput_hr = report.items_total / (report.window.duration_seconds / 3600.0)
        lines.append(f"- **Throughput:** {throughput_hr:.2f} items/hr")
    lines.append(_cw_period_line(report.cloudwatch_period_seconds))
    _append_flags(lines, report.flags)
    _append_workflows(lines, report.workflows)
    _append_activities(lines, report.activities)
    _append_hardware(lines, report.hardware)
    _append_gpu(lines, report.gpu)
    return "\n".join(lines) + "\n"


def render_comparison_markdown(report: ComparisonReport) -> str:
    b = report.batch
    l = report.live
    lines: list[str] = []
    lines.append("# Live vs Batch comparison")
    lines.append("")
    lines.append(f"- **Generated:** {report.generated_at.isoformat()}")
    lines.append(f"- **Batch:** {b.batch_id} ({_fmt_duration(b.duration_seconds) if b.duration_seconds else '—'})")
    lines.append(
        f"- **Live window:** {l.window.since.isoformat()} → {l.window.until.isoformat()} "
        f"({_fmt_duration(l.window.duration_seconds)})"
    )
    lines.append("")

    _append_section_header(lines, "Items / throughput")
    items_rows: list[tuple[str, float, float]] = [
        ("Items total", float(l.items_total), float(b.items_total)),
        ("Items succeeded", float(l.items_succeeded), float(b.items_succeeded)),
        ("Items failed", float(l.items_failed), float(b.items_failed)),
        (
            "Throughput (items/hr)",
            _per_hour(l.items_total, l.window.duration_seconds),
            _per_hour(b.items_total, b.duration_seconds or 0.0),
        ),
        (
            "Error rate (%)",
            _rate(l.items_failed, l.items_total) * 100.0,
            _rate(b.items_failed, b.items_total) * 100.0,
        ),
    ]
    _append_compare_table(lines, "Metric", items_rows)

    _append_section_header(lines, "ProcessPdfWorkflow duration (s)")
    l_pdf = _find_workflow(l.workflows, "ProcessPdfWorkflow")
    b_pdf = _find_workflow(b.workflows, "ProcessPdfWorkflow")
    _append_stat_compare(
        lines,
        l_pdf.duration_seconds if l_pdf else None,
        b_pdf.duration_seconds if b_pdf else None,
    )

    _append_section_header(lines, "Activity start-to-close p50 (s) — joined by activity_type")
    _append_activity_compare(lines, l.activities, b.activities, "start_to_close_seconds", "p50")

    _append_section_header(lines, "Activity start-to-close p95 (s) — joined by activity_type")
    _append_activity_compare(lines, l.activities, b.activities, "start_to_close_seconds", "p95")

    _append_section_header(lines, "Hardware (aggregated across all instances per side)")
    hw_rows: list[tuple[str, float, float]] = [
        ("CPU active % (avg of p95s)", _avg_p95(l.hardware, "cpu_active_percent"), _avg_p95(b.hardware, "cpu_active_percent")),
        ("Memory used % (avg of p95s)", _avg_p95(l.hardware, "memory_used_percent"), _avg_p95(b.hardware, "memory_used_percent")),
        ("Worker CPU % (avg of p95s)", _avg_p95(l.hardware, "worker_cpu_percent"), _avg_p95(b.hardware, "worker_cpu_percent")),
        ("Net sent KiB/s (avg of p95s)", _avg_p95(l.hardware, "net_bytes_sent_per_second") / 1024.0, _avg_p95(b.hardware, "net_bytes_sent_per_second") / 1024.0),
        ("Net recv KiB/s (avg of p95s)", _avg_p95(l.hardware, "net_bytes_recv_per_second") / 1024.0, _avg_p95(b.hardware, "net_bytes_recv_per_second") / 1024.0),
        ("Instances seen (count)", float(len(l.hardware)), float(len(b.hardware))),
    ]
    _append_compare_table(lines, "Metric", hw_rows)

    _append_section_header(lines, "GPU — vLLM box (the headline efficiency metric)")
    gpu_rows: list[tuple[str, float, float]] = [
        ("GPU util % (avg of p95s)", _avg_p95_gpu(l.gpu, "utilization_percent"), _avg_p95_gpu(b.gpu, "utilization_percent")),
        ("GPU memory used MiB (avg of p95s)", _avg_p95_gpu(l.gpu, "memory_used_mib"), _avg_p95_gpu(b.gpu, "memory_used_mib")),
        ("GPU instances seen", float(len(l.gpu)), float(len(b.gpu))),
    ]
    _append_compare_table(lines, "Metric", gpu_rows)

    return "\n".join(lines) + "\n"


def _append_section_header(lines: list[str], title: str) -> None:
    lines.append("")
    lines.append(f"## {title}")
    lines.append("")


def _append_flags(lines: list[str], flags: list[str]) -> None:
    if not flags:
        return
    lines.append("")
    lines.append("## Flags")
    for f in flags:
        lines.append(f"- {f}")


def _append_workflows(lines: list[str], workflows: list[WorkflowStats]) -> None:
    lines.append("")
    lines.append("## Workflows")
    lines.append("")
    lines.append("| Type | Count | OK | Failed | Other | Duration p50 / p95 / max (s) |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for w in workflows:
        lines.append(
            f"| {w.workflow_type} | {w.count} | {w.success_count} | "
            f"{w.failure_count} | {w.other_count} | {_fmt_stat(w.duration_seconds)} |"
        )


def _append_activities(lines: list[str], activities: list[ActivityStats]) -> None:
    lines.append("")
    lines.append("## Activities")
    lines.append("")
    lines.append(
        "| Type | Count | OK | Failed | Retries | "
        "Start→Close p50 / p95 / max (s) | Sched→Close p50 / p95 / max (s) |"
    )
    lines.append("|---|---:|---:|---:|---:|---|---|")
    for a in activities:
        lines.append(
            f"| {a.activity_type} | {a.count} | {a.success_count} | "
            f"{a.failure_count} | {a.retry_count} | "
            f"{_fmt_stat(a.start_to_close_seconds)} | "
            f"{_fmt_stat(a.schedule_to_close_seconds)} |"
        )


def _append_hardware(lines: list[str], hardware: list[WorkerInstanceStats]) -> None:
    lines.append("")
    lines.append("## Hardware (per instance)")
    if not hardware:
        lines.append("")
        lines.append("_No hardware data — CloudWatch returned no metrics for the window._")
        return
    lines.append("")
    for h in hardware:
        lines.append(f"### {h.instance_id}")
        if h.first_seen_at and h.last_seen_at:
            lines.append(f"_Active: {h.first_seen_at.isoformat()} → {h.last_seen_at.isoformat()}_")
        lines.append("")
        lines.append("| Metric | p50 | p95 | max |")
        lines.append("|---|---:|---:|---:|")
        lines.append(f"| CPU active % | {_fmt_stat_short(h.cpu_active_percent)} |")
        lines.append(f"| Memory used % | {_fmt_stat_short(h.memory_used_percent)} |")
        lines.append(f"| Worker CPU % | {_fmt_stat_short(h.worker_cpu_percent)} |")
        lines.append(f"| Worker RSS (MiB) | {_fmt_stat_short(_scale(h.worker_memory_rss_bytes, 1.0 / (1024 * 1024)))} |")
        lines.append(f"| Net sent (KiB/s) | {_fmt_stat_short(_scale(h.net_bytes_sent_per_second, 1.0 / 1024))} |")
        lines.append(f"| Net recv (KiB/s) | {_fmt_stat_short(_scale(h.net_bytes_recv_per_second, 1.0 / 1024))} |")
        lines.append("")


def _append_gpu(lines: list[str], gpu: list[GpuStats]) -> None:
    lines.append("")
    lines.append("## GPU (vLLM)")
    if not gpu:
        lines.append("")
        lines.append("_No GPU data — the nvidia-smi sidecar may not be running on the vLLM box._")
        return
    lines.append("")
    for g in gpu:
        lines.append(f"### {g.instance_id}")
        if g.memory_total_mib:
            lines.append(f"_Total VRAM: {g.memory_total_mib:.0f} MiB_")
        lines.append("")
        lines.append("| Metric | p50 | p95 | max |")
        lines.append("|---|---:|---:|---:|")
        lines.append(f"| Utilization % | {_fmt_stat_short(g.utilization_percent)} |")
        lines.append(f"| Memory used (MiB) | {_fmt_stat_short(g.memory_used_mib)} |")
        lines.append("")


def _append_compare_table(
    lines: list[str], label: str, rows: list[tuple[str, float, float]],
) -> None:
    lines.append(f"| {label} | Live | Batch | Δ (live−batch) | Ratio (live/batch) |")
    lines.append("|---|---:|---:|---:|---:|")
    for name, live_v, batch_v in rows:
        delta = live_v - batch_v
        ratio = (live_v / batch_v) if batch_v not in (0.0, 0) else float("nan")
        lines.append(
            f"| {name} | {_fmt_num(live_v)} | {_fmt_num(batch_v)} | "
            f"{_fmt_signed(delta)} | {_fmt_ratio(ratio)} |"
        )


def _append_stat_compare(
    lines: list[str],
    live_stat: Optional[StatSummary],
    batch_stat: Optional[StatSummary],
) -> None:
    rows: list[tuple[str, float, float]] = []
    for pct in ("p50", "p95", "p99", "max"):
        l_v = getattr(live_stat, pct) if live_stat else 0.0
        b_v = getattr(batch_stat, pct) if batch_stat else 0.0
        rows.append((pct, float(l_v), float(b_v)))
    rows.append((
        "count", float(live_stat.count if live_stat else 0),
        float(batch_stat.count if batch_stat else 0),
    ))
    _append_compare_table(lines, "Metric", rows)


def _append_activity_compare(
    lines: list[str],
    live_acts: list[ActivityStats],
    batch_acts: list[ActivityStats],
    stat_field: str,
    pct_field: str,
) -> None:
    l_by_type = {a.activity_type: a for a in live_acts}
    b_by_type = {a.activity_type: a for a in batch_acts}
    types = sorted(set(l_by_type) | set(b_by_type))
    rows: list[tuple[str, float, float]] = []
    for t in types:
        l_stat = getattr(l_by_type.get(t), stat_field, None) if l_by_type.get(t) else None
        b_stat = getattr(b_by_type.get(t), stat_field, None) if b_by_type.get(t) else None
        l_v = getattr(l_stat, pct_field, 0.0) if l_stat else 0.0
        b_v = getattr(b_stat, pct_field, 0.0) if b_stat else 0.0
        rows.append((t, float(l_v), float(b_v)))
    if not rows:
        lines.append("_No activities observed on either side._")
        return
    _append_compare_table(lines, "Activity", rows)


def _find_workflow(workflows: list[WorkflowStats], name: str) -> Optional[WorkflowStats]:
    for w in workflows:
        if w.workflow_type == name:
            return w
    return None


def _avg_p95(hardware: list[WorkerInstanceStats], field: str) -> float:
    vals: list[float] = []
    for h in hardware:
        s: Optional[StatSummary] = getattr(h, field)
        if s is not None:
            vals.append(s.p95)
    return sum(vals) / len(vals) if vals else 0.0


def _avg_p95_gpu(gpu: list[GpuStats], field: str) -> float:
    vals: list[float] = []
    for g in gpu:
        s: Optional[StatSummary] = getattr(g, field)
        if s is not None:
            vals.append(s.p95)
    return sum(vals) / len(vals) if vals else 0.0


def _per_hour(count: int, duration_s: float) -> float:
    return (count / (duration_s / 3600.0)) if duration_s > 0 else 0.0


def _rate(num: int, denom: int) -> float:
    return (num / denom) if denom else 0.0


def _cw_period_line(period: int) -> str:
    return (
        f"- **CloudWatch resolution:** {period}s"
        if period
        else "- **CloudWatch resolution:** n/a (hardware fetch skipped)"
    )


def _fmt_stat(s: Optional[StatSummary]) -> str:
    if s is None or s.count == 0:
        return "—"
    return f"{s.p50:.1f} / {s.p95:.1f} / {s.max:.1f}  (n={s.count})"


def _fmt_stat_short(s: Optional[StatSummary]) -> str:
    if s is None or s.count == 0:
        return "— | — | —"
    return f"{s.p50:.1f} | {s.p95:.1f} | {s.max:.1f}"


def _fmt_num(v: float) -> str:
    if v == int(v) and abs(v) < 1e6:
        return f"{int(v)}"
    return f"{v:.2f}"


def _fmt_signed(v: float) -> str:
    sign = "+" if v > 0 else ("−" if v < 0 else "")
    return f"{sign}{abs(v):.2f}" if v != 0 else "0"


def _fmt_ratio(v: float) -> str:
    if v != v:                    # NaN
        return "—"
    return f"{v:.2f}×"


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.2f}h"


def _scale(s: Optional[StatSummary], k: float) -> Optional[StatSummary]:
    if s is None:
        return None
    return StatSummary(
        count=s.count, max=s.max * k, p50=s.p50 * k, p95=s.p95 * k, p99=s.p99 * k,
    )
