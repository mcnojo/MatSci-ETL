"""Block until activity pollers register on each named task queue.

Used after scale_fleet_up_activity to gate the workflow's fan-out on real
workers being present. Timeout means the workers never registered (bootstrap
failure, AMI broken, quota silently denied) — non-retryable since another
attempt won't change the underlying cause.

Reads TEMPORAL_ADDRESS / TEMPORAL_NAMESPACE from the worker process env
(set by prod/live/worker.py at startup), so the activity can connect a fresh
Temporal client to drive DescribeTaskQueue.
"""

import asyncio
import os
import time

from pydantic import BaseModel, ConfigDict
from temporalio import activity
from temporalio.api.enums.v1 import TaskQueueKind, TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from shared.temporal_client import connect_temporal


class AwaitPollersInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    namespace: str
    task_queues: list[str]
    timeout_s: int = 600
    poll_interval_s: float = 5.0


class AwaitPollersOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ready_queues: list[str]
    elapsed_s: float


async def _has_activity_pollers(client: Client, namespace: str, task_queue: str) -> bool:
    resp = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=namespace,
            task_queue=TaskQueue(name=task_queue, kind=TaskQueueKind.TASK_QUEUE_KIND_NORMAL),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        ),
    )
    return len(resp.pollers) >= 1


@activity.defn(name="batch_await-pollers")
async def await_pollers_activity(input: AwaitPollersInput) -> AwaitPollersOutput:
    address = os.environ.get("TEMPORAL_ADDRESS")
    if not address:
        raise ApplicationError(
            "TEMPORAL_ADDRESS env var not set on worker — cannot poll task queues",
            non_retryable=True,
        )
    client = await connect_temporal(address, namespace=input.namespace)

    targets = set(input.task_queues)
    ready: set[str] = set()
    start = time.monotonic()
    deadline = start + input.timeout_s

    while time.monotonic() < deadline:
        activity.heartbeat({"ready": sorted(ready), "pending": sorted(targets - ready)})
        pending = sorted(targets - ready)
        if not pending:
            break
        results = await asyncio.gather(
            *(_has_activity_pollers(client, input.namespace, q) for q in pending),
        )
        for q, ok in zip(pending, results):
            if ok:
                ready.add(q)
        if ready == targets:
            break
        await asyncio.sleep(input.poll_interval_s)

    if ready != targets:
        raise ApplicationError(
            f"timeout waiting for pollers on {sorted(targets - ready)} "
            f"after {input.timeout_s}s",
            non_retryable=True,
        )

    return AwaitPollersOutput(
        ready_queues=sorted(ready), elapsed_s=time.monotonic() - start,
    )
