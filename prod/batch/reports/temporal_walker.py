"""Walk BatchRunWorkflow + reachable children, aggregate activity/workflow stats."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowExecutionStatus

from .models import ActivityStats, StatSummary, WorkflowStats

_ACTIVITY_TERMINAL = {
    EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED,
    EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED,
    EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT,
    EventType.EVENT_TYPE_ACTIVITY_TASK_CANCELED,
}

_HISTORY_FETCH_CONCURRENCY = 20


@dataclass
class _ActivityRecord:
    activity_type: str
    schedule_time: datetime
    last_start_time: Optional[datetime] = None
    close_time: Optional[datetime] = None
    attempts: int = 0
    outcome: Optional[str] = None               # success | failure | timed_out | canceled


@dataclass
class _WorkflowRecord:
    workflow_id: str
    workflow_type: str
    started_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    status: Optional[WorkflowExecutionStatus] = None
    activities: list[_ActivityRecord] = field(default_factory=list)
    child_workflow_ids: list[str] = field(default_factory=list)


async def walk_batch(client: Client, batch_workflow_id: str) -> list[_WorkflowRecord]:
    """DFS in discovery order (parent before children) for stable aggregation."""
    sem = asyncio.Semaphore(_HISTORY_FETCH_CONCURRENCY)
    visited: set[str] = set()
    order: list[_WorkflowRecord] = []

    async def visit(workflow_id: str) -> None:
        if workflow_id in visited:
            return
        visited.add(workflow_id)
        async with sem:
            rec = await _fetch_workflow(client, workflow_id)
        order.append(rec)
        if rec.child_workflow_ids:
            async with asyncio.TaskGroup() as tg:
                for child_id in rec.child_workflow_ids:
                    tg.create_task(visit(child_id))

    await visit(batch_workflow_id)
    return order


async def _fetch_workflow(client: Client, workflow_id: str) -> _WorkflowRecord:
    handle = client.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    rec = _WorkflowRecord(
        workflow_id=workflow_id,
        workflow_type=desc.workflow_type or "Unknown",
        started_at=desc.start_time,
        closed_at=desc.close_time,
        status=desc.status,
    )

    schedule_id_to_record: dict[int, _ActivityRecord] = {}
    async for event in handle.fetch_history_events():
        et = event.event_type

        if et == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            ar = _ActivityRecord(
                activity_type=attrs.activity_type.name,
                schedule_time=_to_aware(event.event_time.ToDatetime()),
            )
            schedule_id_to_record[event.event_id] = ar
            rec.activities.append(ar)

        elif et == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            attrs = event.activity_task_started_event_attributes
            ar = schedule_id_to_record.get(attrs.scheduled_event_id)
            if ar is not None:
                ar.last_start_time = _to_aware(event.event_time.ToDatetime())
                ar.attempts = max(ar.attempts, attrs.attempt)  # Temporal attempts 1-indexed

        elif et in _ACTIVITY_TERMINAL:
            attrs = _terminal_attrs(event, et)
            ar = schedule_id_to_record.get(attrs.scheduled_event_id)
            if ar is not None:
                ar.close_time = _to_aware(event.event_time.ToDatetime())
                ar.outcome = _terminal_outcome(et)

        elif et == EventType.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_STARTED:
            attrs = event.child_workflow_execution_started_event_attributes
            rec.child_workflow_ids.append(attrs.workflow_execution.workflow_id)

    return rec


def _to_aware(dt: datetime) -> datetime:
    # Timestamp.ToDatetime() returns naive UTC.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _terminal_attrs(event, et: int):
    if et == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
        return event.activity_task_completed_event_attributes
    if et == EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED:
        return event.activity_task_failed_event_attributes
    if et == EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT:
        return event.activity_task_timed_out_event_attributes
    return event.activity_task_canceled_event_attributes


def _terminal_outcome(et: int) -> str:
    return {
        EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED: "success",
        EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED: "failure",
        EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT: "timed_out",
        EventType.EVENT_TYPE_ACTIVITY_TASK_CANCELED: "canceled",
    }[et]


def aggregate_workflows(records: list[_WorkflowRecord]) -> list[WorkflowStats]:
    by_type: dict[str, list[_WorkflowRecord]] = defaultdict(list)
    for rec in records:
        by_type[rec.workflow_type].append(rec)

    out: list[WorkflowStats] = []
    for workflow_type, group in by_type.items():
        success = sum(1 for r in group if r.status == WorkflowExecutionStatus.COMPLETED)
        failure = sum(1 for r in group if r.status == WorkflowExecutionStatus.FAILED)
        durations = [
            (r.closed_at - r.started_at).total_seconds()
            for r in group
            if r.started_at and r.closed_at
        ]
        out.append(
            WorkflowStats(
                workflow_type=workflow_type,
                count=len(group),
                success_count=success,
                failure_count=failure,
                other_count=len(group) - success - failure,
                duration_seconds=_summarize(durations),
            )
        )
    return sorted(out, key=lambda w: w.workflow_type)


def aggregate_activities(records: list[_WorkflowRecord]) -> list[ActivityStats]:
    by_type: dict[str, list[_ActivityRecord]] = defaultdict(list)
    for rec in records:
        for ar in rec.activities:
            by_type[ar.activity_type].append(ar)

    out: list[ActivityStats] = []
    for activity_type, group in by_type.items():
        success = sum(1 for a in group if a.outcome == "success")
        failure = sum(1 for a in group if a.outcome in ("failure", "timed_out"))
        retries = sum(max(0, a.attempts - 1) for a in group)
        sched_to_close = [
            (a.close_time - a.schedule_time).total_seconds()
            for a in group
            if a.close_time and a.schedule_time
        ]
        start_to_close = [
            (a.close_time - a.last_start_time).total_seconds()
            for a in group
            if a.close_time and a.last_start_time
        ]
        out.append(
            ActivityStats(
                activity_type=activity_type,
                count=len(group),
                success_count=success,
                failure_count=failure,
                retry_count=retries,
                schedule_to_close_seconds=_summarize(sched_to_close),
                start_to_close_seconds=_summarize(start_to_close),
            )
        )
    return sorted(out, key=lambda a: a.activity_type)


def _summarize(values: list[float]) -> Optional[StatSummary]:
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
    # Linear-interpolation percentile; matches numpy default closely enough.
    if len(sv) == 1:
        return sv[0]
    rank = (pct / 100.0) * (len(sv) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sv) - 1)
    return sv[lo] + (sv[hi] - sv[lo]) * (rank - lo)


def batch_window(
    records: list[_WorkflowRecord], batch_workflow_id: str,
) -> tuple[Optional[datetime], Optional[datetime], Optional[WorkflowExecutionStatus]]:
    """Pull the BatchRunWorkflow's started_at / closed_at / status."""
    for rec in records:
        if rec.workflow_id == batch_workflow_id:
            return rec.started_at, rec.closed_at, rec.status
    return None, None, None
