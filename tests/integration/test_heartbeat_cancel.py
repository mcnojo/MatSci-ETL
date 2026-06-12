"""Verify the asyncio.shield + _await_with_heartbeats interaction with
Temporal's activity cancellation.

The concern: when Temporal cancels an activity mid-flight (via the workflow,
via graceful worker shutdown, or via a Spot interruption SIGTERM), the
helper must (a) propagate cancellation through the shield so the outer
activity coroutine raises CancelledError, AND (b) actually cancel the
underlying inner task so its resources are released and its finally-blocks
run.

This test exercises that path against a real Temporal server (the local
docker-compose stack). It registers a self-contained worker + workflow +
activity in this process under a unique task queue (so it can't collide
with the user's regular worker) and verifies cancellation propagation
end-to-end.

The workflow module here ONLY uses the activity by string name, not by
Python reference, so the sandbox does not need to import the activity's
heavy transitive deps (openai, boto3, pymupdf).

Run: python -m tests.integration.test_heartbeat_cancel
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import uuid
from datetime import timedelta

from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import CancelledError as TemporalCancelledError
from temporalio.worker import Worker

from shared.temporal.client import connect_temporal

with workflow.unsafe.imports_passed_through():
    from tests.integration._heartbeat_cancel_activity import (
        HangInput,
        hang_with_heartbeats,
        inner_cancelled_event,
        inner_finally_ran_event,
        reset_flags,
    )


@workflow.defn
class CancelHangWorkflow:
    """Workflow that runs hang_with_heartbeats and re-raises whatever it gets.

    The test issues a cancel against this workflow's execution; Temporal
    propagates the cancellation to the running activity.
    """

    @workflow.run
    async def run(self, input: HangInput) -> str:
        return await workflow.execute_activity(
            "hang-with-heartbeats",
            input,
            start_to_close_timeout=timedelta(seconds=120),
            heartbeat_timeout=timedelta(seconds=15),
        )


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _run() -> int:
    host = os.environ.get("TEMPORAL_HOST", "localhost")
    port = int(os.environ.get("TEMPORAL_PORT", "7233"))
    if not _port_open(host, port):
        print(f"SKIP: Temporal not reachable at {host}:{port}")
        return 0

    reset_flags()

    client = await connect_temporal(f"{host}:{port}")
    task_queue = f"heartbeat-cancel-test-{uuid.uuid4().hex[:8]}"
    workflow_id = f"hb-cancel-{uuid.uuid4().hex[:8]}"

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[CancelHangWorkflow],
        activities=[hang_with_heartbeats],
        max_concurrent_activities=2,
    )

    print(f"task_queue: {task_queue}")
    print(f"workflow_id: {workflow_id}")

    async def drive() -> tuple[bool, str]:
        """Start the workflow, give it ~3s to enter the activity, then cancel."""
        handle = await client.start_workflow(
            CancelHangWorkflow.run,
            HangInput(sleep_for_s=30.0),
            id=workflow_id,
            task_queue=task_queue,
            execution_timeout=timedelta(seconds=180),
        )
        await asyncio.sleep(3.0)
        print("  -> cancelling workflow")
        await handle.cancel()
        try:
            result = await asyncio.wait_for(handle.result(), timeout=30.0)
        except WorkflowFailureError as exc:
            return (
                isinstance(exc.cause, TemporalCancelledError),
                f"WorkflowFailureError cause={type(exc.cause).__name__}",
            )
        except asyncio.TimeoutError:
            return (False, "workflow did not finish within 30s after cancel")
        return (False, f"workflow completed without cancel: {result!r}")

    worker_task = asyncio.create_task(worker.run())
    try:
        cancel_propagated, message = await drive()
    finally:
        await worker.shutdown()
        await worker_task

    print(f"  workflow outcome: {message}")
    await asyncio.sleep(0.5)  # let finally blocks settle

    failures: list[str] = []
    if not cancel_propagated:
        failures.append(f"workflow did not raise Temporal CancelledError: {message}")
    if not inner_cancelled_event.is_set():
        failures.append(
            "inner task did NOT receive asyncio.CancelledError — the shield "
            "swallowed the cancel and the inner coroutine kept running."
        )
    if not inner_finally_ran_event.is_set():
        failures.append(
            "inner task's finally block did not run — resources may leak on "
            "Temporal-initiated cancellation."
        )

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        "\nPASS: workflow cancelled, inner task observed CancelledError, "
        "finally block ran"
    )
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
