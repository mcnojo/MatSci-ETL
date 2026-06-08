"""Pydantic models for the end-of-batch report."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class StatSummary(BaseModel):
    """count/max/p50/p95 feed the renderer; p99 feeds the long-tail flag."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int
    max: float
    p50: float
    p95: float
    p99: float


class ActivityStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    activity_type: str
    count: int
    success_count: int
    failure_count: int
    retry_count: int                            # sum(max_attempt - 1)
    schedule_to_close_seconds: Optional[StatSummary] = None  # queue + execute
    start_to_close_seconds: Optional[StatSummary] = None     # execute only


class WorkflowStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_type: str
    count: int
    success_count: int
    failure_count: int
    other_count: int                            # canceled, terminated, timed-out, running
    duration_seconds: Optional[StatSummary] = None


class WorkerInstanceStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    cpu_active_percent: Optional[StatSummary] = None        # 100 - idle
    memory_used_percent: Optional[StatSummary] = None
    worker_cpu_percent: Optional[StatSummary] = None        # procstat for python worker
    worker_memory_rss_bytes: Optional[StatSummary] = None
    net_bytes_sent_per_second: Optional[StatSummary] = None # summed across non-lo ifaces
    net_bytes_recv_per_second: Optional[StatSummary] = None


class BatchReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    status: str                                 # COMPLETED, FAILED, RUNNING, ...
    started_at: datetime
    closed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    items_total: int
    items_succeeded: int
    items_failed: int
    workflows: list[WorkflowStats]
    activities: list[ActivityStats]
    hardware: list[WorkerInstanceStats]
    flags: list[str]                            # human-readable anomaly hints
    cloudwatch_period_seconds: int              # CW resolution used during fetch
