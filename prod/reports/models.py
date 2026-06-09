"""Pydantic models for batch / live / comparison reports."""

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


class GpuStats(BaseModel):
    """GPU utilization on a vLLM instance, from the nvidia-smi → CW sidecar."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str
    utilization_percent: Optional[StatSummary] = None
    memory_used_mib: Optional[StatSummary] = None
    memory_total_mib: Optional[float] = None    # nvidia-smi reports constant per device


class WorkerInstanceStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    cpu_active_percent: Optional[StatSummary] = None        # 100 - idle
    memory_used_percent: Optional[StatSummary] = None
    worker_cpu_percent: Optional[StatSummary] = None        # procstat for the worker process
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
    gpu: list[GpuStats]
    flags: list[str]                            # human-readable anomaly hints
    cloudwatch_period_seconds: int              # CW resolution used during fetch


class LiveWindow(BaseModel):
    """A closed [since, until] interval over which live runs are aggregated."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    since: datetime
    until: datetime

    @property
    def duration_seconds(self) -> float:
        return (self.until - self.since).total_seconds()


class LiveReport(BaseModel):
    """Window aggregate of standalone ProcessPdfWorkflow runs."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: LiveWindow
    items_total: int
    items_succeeded: int
    items_failed: int
    workflows: list[WorkflowStats]
    activities: list[ActivityStats]
    hardware: list[WorkerInstanceStats]
    gpu: list[GpuStats]
    flags: list[str]
    cloudwatch_period_seconds: int


class ComparisonReport(BaseModel):
    """Side-by-side LiveReport vs BatchReport — shape preserved so renderer
    can iterate the same metric categories from both sides."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    batch: BatchReport
    live: LiveReport
