"""Per-worker + per-GPU CloudWatch metrics → one StatSummary per metric per instance."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3

from .models import GpuStats, StatSummary, WorkerInstanceStats

BATCH_NAMESPACE = "OCR/Batch/Worker"
LIVE_NAMESPACE = "OCR/Live/Worker"
GPU_NAMESPACE = "OCR/vLLM/GPU"

_WORKER_PROCESS_PATTERNS = {
    # Each motif's worker process — used as the procstat `pattern` dimension.
    BATCH_NAMESPACE: "prod.batch.worker",
    LIVE_NAMESPACE: "prod.live.worker",
}


@dataclass(frozen=True)
class _MetricResult:
    timestamps: list[datetime]
    values: list[float]


def fetch_hardware_stats(
    region: str,
    start: datetime,
    end: datetime,
    *,
    namespace: str,
) -> tuple[list[WorkerInstanceStats], int]:
    """Returns (per-instance stats, CW period used)."""
    cw = boto3.client("cloudwatch", region_name=region)
    period = _pick_period(end)
    query_start, query_end = _padded_window(start, end)

    instance_ids = _enumerate_instances(cw, namespace, "procstat_cpu_usage")
    if not instance_ids:
        return [], period

    stats: list[WorkerInstanceStats] = []
    process_pattern = _WORKER_PROCESS_PATTERNS[namespace]
    for inst in instance_ids:
        interfaces = _enumerate_interfaces(cw, namespace, inst)
        queries = _build_worker_queries(inst, interfaces, namespace, period, process_pattern)
        results = _run_queries(cw, queries, query_start, query_end)
        stats.append(_aggregate_worker(inst, results, interfaces))
    return stats, period


async def fetch_hardware_stats_async(
    region: str, start: datetime, end: datetime, *, namespace: str,
) -> tuple[list[WorkerInstanceStats], int]:
    return await asyncio.to_thread(fetch_hardware_stats, region, start, end, namespace=namespace)


def _build_worker_queries(
    instance_id: str,
    interfaces: list[str],
    namespace: str,
    period: int,
    process_pattern: str,
) -> list[dict]:
    base_inst = [{"Name": "InstanceId", "Value": instance_id}]
    procstat_dims = [
        {"Name": "pattern", "Value": process_pattern},
        {"Name": "pid_finder", "Value": "native"},
        *base_inst,
    ]
    queries = [
        _query("cpu_active", namespace, "cpu_usage_active",
               [{"Name": "cpu", "Value": "cpu-total"}, *base_inst], period),
        _query("mem_used_pct", namespace, "mem_used_percent", base_inst, period),
        _query("worker_cpu", namespace, "procstat_cpu_usage", procstat_dims, period),
        _query("worker_rss", namespace, "procstat_memory_rss", procstat_dims, period),
    ]
    # net_bytes_* are cumulative counters from /proc/net/dev — wrap each in
    # RATE() so consumers see bytes/sec.
    for i, iface in enumerate(interfaces):
        iface_dims = [{"Name": "interface", "Value": iface}, *base_inst]
        raw_sent = f"raw_net_sent_{i}"
        raw_recv = f"raw_net_recv_{i}"
        queries.append(_query(raw_sent, namespace, "net_bytes_sent", iface_dims, period, return_data=False))
        queries.append(_query(raw_recv, namespace, "net_bytes_recv", iface_dims, period, return_data=False))
        queries.append({"Id": f"net_sent_{i}", "Expression": f"RATE({raw_sent})", "ReturnData": True})
        queries.append({"Id": f"net_recv_{i}", "Expression": f"RATE({raw_recv})", "ReturnData": True})
    return queries


def _aggregate_worker(
    instance_id: str,
    results: dict[str, _MetricResult],
    interfaces: list[str],
) -> WorkerInstanceStats:
    cpu_active = summarize(results.get("cpu_active", _MetricResult([], [])).values)
    mem_used = summarize(results.get("mem_used_pct", _MetricResult([], [])).values)
    worker_cpu = summarize(results.get("worker_cpu", _MetricResult([], [])).values)
    worker_rss = summarize(results.get("worker_rss", _MetricResult([], [])).values)

    sent_per_s = _aggregate_net(results, "net_sent_", len(interfaces))
    recv_per_s = _aggregate_net(results, "net_recv_", len(interfaces))

    first, last = _earliest_and_latest_timestamp(results)
    return WorkerInstanceStats(
        instance_id=instance_id,
        first_seen_at=first,
        last_seen_at=last,
        cpu_active_percent=cpu_active,
        memory_used_percent=mem_used,
        worker_cpu_percent=worker_cpu,
        worker_memory_rss_bytes=worker_rss,
        net_bytes_sent_per_second=sent_per_s,
        net_bytes_recv_per_second=recv_per_s,
    )


def _aggregate_net(
    results: dict[str, _MetricResult], prefix: str, n_interfaces: int,
) -> Optional[StatSummary]:
    # Inputs are already bytes/sec (RATE() applied at query-build); sum
    # across non-loopback interfaces at matching timestamps, then summarize.
    if n_interfaces == 0:
        return None
    by_ts: dict[datetime, float] = defaultdict(float)
    for i in range(n_interfaces):
        mr = results.get(f"{prefix}{i}")
        if mr is None:
            continue
        for ts, v in zip(mr.timestamps, mr.values):
            by_ts[ts] += v
    if not by_ts:
        return None
    return summarize(list(by_ts.values()))


def fetch_gpu_stats(
    region: str, start: datetime, end: datetime,
    *, namespace: str = GPU_NAMESPACE,
) -> list[GpuStats]:
    cw = boto3.client("cloudwatch", region_name=region)
    period = _pick_period(end)
    query_start, query_end = _padded_window(start, end)

    instance_ids = _enumerate_instances(cw, namespace, "gpu_utilization_percent")
    if not instance_ids:
        return []

    stats: list[GpuStats] = []
    for inst in instance_ids:
        base_inst = [{"Name": "InstanceId", "Value": inst}]
        queries = [
            _query("util_pct", namespace, "gpu_utilization_percent", base_inst, period),
            _query("mem_used", namespace, "gpu_memory_used_mib", base_inst, period),
            _query("mem_total", namespace, "gpu_memory_total_mib", base_inst, period),
        ]
        results = _run_queries(cw, queries, query_start, query_end)
        mem_total = results.get("mem_total", _MetricResult([], []))
        stats.append(GpuStats(
            instance_id=inst,
            utilization_percent=summarize(results.get("util_pct", _MetricResult([], [])).values),
            memory_used_mib=summarize(results.get("mem_used", _MetricResult([], [])).values),
            memory_total_mib=mem_total.values[-1] if mem_total.values else None,
        ))
    return stats


async def fetch_gpu_stats_async(
    region: str, start: datetime, end: datetime, *, namespace: str = GPU_NAMESPACE,
) -> list[GpuStats]:
    return await asyncio.to_thread(fetch_gpu_stats, region, start, end, namespace=namespace)


def _pick_period(end: datetime) -> int:
    # CW retention curve: 60s for 15d, 300s for 63d, 3600s for 15mo.
    now = datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    age = now - end
    if age <= timedelta(days=15):
        return 60
    if age <= timedelta(days=63):
        return 300
    return 3600


def _padded_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    # CWAgent buffers ~60s; pad so metrics at t=end are visible.
    return start - timedelta(seconds=60), end + timedelta(seconds=120)


def _enumerate_instances(cw, namespace: str, anchor_metric: str) -> list[str]:
    # Pull unique InstanceIds from a known-present metric; sorted for stable report order.
    instance_ids: set[str] = set()
    paginator = cw.get_paginator("list_metrics")
    for page in paginator.paginate(Namespace=namespace, MetricName=anchor_metric):
        for m in page["Metrics"]:
            for d in m["Dimensions"]:
                if d["Name"] == "InstanceId":
                    instance_ids.add(d["Value"])
    return sorted(instance_ids)


def _enumerate_interfaces(cw, namespace: str, instance_id: str) -> list[str]:
    # Non-loopback interfaces with metrics for this instance.
    interfaces: set[str] = set()
    paginator = cw.get_paginator("list_metrics")
    for page in paginator.paginate(Namespace=namespace, MetricName="net_bytes_sent"):
        for m in page["Metrics"]:
            dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
            if dims.get("InstanceId") == instance_id:
                iface = dims.get("interface")
                if iface and iface != "lo":
                    interfaces.add(iface)
    return sorted(interfaces)


def _query(
    qid: str, namespace: str, metric_name: str, dimensions: list[dict],
    period: int, *, return_data: bool = True,
) -> dict:
    return {
        "Id": qid,
        "MetricStat": {
            "Metric": {
                "Namespace": namespace,
                "MetricName": metric_name,
                "Dimensions": dimensions,
            },
            "Period": period,
            "Stat": "Average",
        },
        "ReturnData": return_data,
    }


def _run_queries(
    cw, queries: list[dict], start: datetime, end: datetime,
) -> dict[str, _MetricResult]:
    out: dict[str, _MetricResult] = {}
    for chunk in _chunked(queries, 500):     # GetMetricData hard cap = 500
        resp = cw.get_metric_data(
            MetricDataQueries=chunk,
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampAscending",
        )
        for r in resp.get("MetricDataResults", []):
            out[r["Id"]] = _MetricResult(
                timestamps=list(r.get("Timestamps", [])),
                values=list(r.get("Values", [])),
            )
    return out


def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _earliest_and_latest_timestamp(
    results: dict[str, _MetricResult],
) -> tuple[Optional[datetime], Optional[datetime]]:
    all_ts: list[datetime] = []
    for mr in results.values():
        all_ts.extend(mr.timestamps)
    if not all_ts:
        return None, None
    return min(all_ts), max(all_ts)


def summarize(values: list[float]) -> Optional[StatSummary]:
    if not values:
        return None
    sv = sorted(values)
    return StatSummary(
        count=len(sv),
        max=sv[-1],
        p50=_percentile(sv, 50),
        p95=_percentile(sv, 95),
        p99=_percentile(sv, 99),
    )


def _percentile(sv: list[float], pct: float) -> float:
    if len(sv) == 1:
        return sv[0]
    rank = (pct / 100.0) * (len(sv) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sv) - 1)
    return sv[lo] + (sv[hi] - sv[lo]) * (rank - lo)
