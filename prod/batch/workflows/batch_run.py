"""BatchRunWorkflow — top-level parent for a batch run.

The workflow owns the entire batch lifecycle. A single `cli submit` is
sufficient — no CLI babysitting.

Flow (when input.manages_fleet):
  1. fetch_manifest_activity reads the manifest from S3 (or local) and
     verifies its batch_id matches the input.
  2. scale_fleet_up_activity drives both batch ASGs to their target counts.
  3. await_pollers_activity blocks until activity pollers register on the
     cpu and gpu task queues (non-retryable timeout).
  4. ShardWorkflow children fan out with bounded concurrency.
  5. write_report_activity emits summary.json + per_item.csv + failures.jsonl.
  6. build_report_activity walks Temporal + CloudWatch into report.json +
     report.md (best-effort; failure is logged, doesn't fail the batch).
  7. finally: scale_fleet_down_activity zeroes both ASGs. Uses
     ActivityCancellationType.ABANDON so workflow cancellation does NOT
     cancel the teardown — the activity runs to completion regardless.

When input does not specify a fleet (local dev, `cli submit` against a
pre-running fleet), steps 2/3/7 are skipped. The rich-report step is also
skipped because it depends on a `region` for CloudWatch.

History bound: shards × 3 events (start + complete + meta) ≈ 60 events for
a 20-shard / 1000-PDF batch — well under the 50k event limit. Lifecycle
adds ~8 more events. continue_as_new not required at the 1000-PDF ceiling.

Known caveat: build_report_activity runs while the BatchRunWorkflow is still
RUNNING, so the parent's own duration shows as null in the embedded report.
Operators wanting the final parent duration re-run `cli report <batch_id>`
after completion.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
)

with workflow.unsafe.imports_passed_through():
    from prod.batch.planner import shard_manifest, shard_workflow_id
    from prod.batch.workflows.activities.await_pollers import (
        AwaitPollersInput,
        await_pollers_activity,
    )
    from prod.batch.workflows.activities.build_report import (
        BuildReportInput,
        build_report_activity,
    )
    from prod.batch.workflows.activities.fetch_manifest import (
        FetchManifestInput,
        fetch_manifest_activity,
    )
    from prod.batch.workflows.activities.scale_fleet import (
        ScaleFleetDownInput,
        ScaleFleetUpInput,
        scale_fleet_down_activity,
        scale_fleet_up_activity,
    )
    from prod.batch.workflows.activities.write_report import (
        WriteReportInput,
        write_report_activity,
    )
    from shared.temporal.task_queues import (
        CPU_ACTIVITY_TIMEOUT,
        CPU_HEARTBEAT_TIMEOUT,
        CPU_TASK_QUEUE,
        DEFAULT_RETRY_POLICY,
        GPU_TASK_QUEUE,
        NO_RETRY_POLICY,
        SHARD_WORKFLOW_EXECUTION_TIMEOUT,
    )

    from .models import (
        BatchRunInput,
        BatchRunOutput,
        ItemResult,
        ShardInput,
        ShardOutput,
    )
    from .shard import ShardWorkflow


# Lifecycle activities own their own retry posture. Scale-up bounds short; the
# activity itself classifies quota errors as non_retryable. Await-pollers gets
# the full registration window (caller's timeout_s + headroom) since it's the
# longest legitimate wait in the lifecycle. Teardown gets a generous window so
# transient AWS hiccups don't drop us into a non-zero fleet on cancellation.
_SCALE_UP_TIMEOUT = timedelta(minutes=2)
# scale_fleet_down_activity now waits for AWS to fully remove instances from
# the ASG (closes the race where a fast resubmit rescues about-to-die boxes
# running stale code). Worst case: lifecycle_hook_heartbeat_s (120s drain) +
# AWS termination (~30s) per parallel pool, plus boto retries. 15 min gives
# comfortable headroom even under a stuck-shutdown scenario.
_SCALE_DOWN_TIMEOUT = timedelta(minutes=15)
_AWAIT_POLLERS_BUFFER = timedelta(minutes=2)        # added to caller's timeout_s
_BUILD_REPORT_TIMEOUT = timedelta(minutes=15)


@workflow.defn
class BatchRunWorkflow:
    """Parent workflow that fans a manifest out to ShardWorkflow children
    and owns the batch fleet's lifecycle."""

    @workflow.run
    async def run(self, input: BatchRunInput) -> BatchRunOutput:
        # Stage 1: fetch + validate manifest.
        fetch_out = await workflow.execute_activity(
            fetch_manifest_activity,
            FetchManifestInput(manifest_uri=input.manifest_uri),
            task_queue=CPU_TASK_QUEUE,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        manifest = fetch_out.manifest
        if manifest.batch_id != input.batch_id:
            raise ApplicationError(
                f"manifest batch_id ({manifest.batch_id!r}) does not match "
                f"input batch_id ({input.batch_id!r}) — refusing to run",
                non_retryable=True,
            )

        merged_config = _merge_config(input.pipeline_config, manifest.config_overrides)
        shards = shard_manifest(manifest, shard_size=input.shard_size)
        workflow.logger.info(
            "batch %s: %d items -> %d shards (shard_size=%d, shards_in_flight=%d, manages_fleet=%s)",
            input.batch_id, len(manifest.items), len(shards),
            input.shard_size, input.shards_in_flight, input.manages_fleet,
        )

        # Lifecycle is all-or-nothing: scale up + wait for pollers + scale down
        # only when the caller supplied fleet coords. Local dev and `cli submit`
        # against a pre-running fleet bypass these steps.
        manages_fleet = input.manages_fleet
        cancelled = False
        main_exc: BaseException | None = None
        result: BatchRunOutput | None = None

        try:
            if manages_fleet:
                # Stage 2: scale fleet up.
                await workflow.execute_activity(
                    scale_fleet_up_activity,
                    ScaleFleetUpInput(
                        region=input.region,
                        cpu_queue_asg_name=input.cpu_queue_asg_name,
                        cpu_queue_desired=input.cpu_queue_desired,
                        gpu_queue_asg_name=input.gpu_queue_asg_name,
                        gpu_queue_desired=input.gpu_queue_desired,
                    ),
                    task_queue=CPU_TASK_QUEUE,
                    start_to_close_timeout=_SCALE_UP_TIMEOUT,
                    heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
                    retry_policy=DEFAULT_RETRY_POLICY,
                )

                # Stage 3: wait for pollers to register.
                await workflow.execute_activity(
                    await_pollers_activity,
                    AwaitPollersInput(
                        namespace=workflow.info().namespace,
                        task_queues=[CPU_TASK_QUEUE, GPU_TASK_QUEUE],
                        timeout_s=input.worker_registration_timeout_s,
                    ),
                    task_queue=CPU_TASK_QUEUE,
                    start_to_close_timeout=(
                        timedelta(seconds=input.worker_registration_timeout_s) + _AWAIT_POLLERS_BUFFER
                    ),
                    heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
                    retry_policy=NO_RETRY_POLICY,    # activity classifies its own retry-ability
                )

            # Stage 4: fan out shards.
            shard_results = await self._fan_out_shards(
                input.batch_id, shards, merged_config,
                input.shards_in_flight, input.pdfs_per_shard_in_flight,
            )

            # Stage 5: aggregate + write summary report.
            all_items: list[ItemResult] = []
            for shard_out in shard_results:
                if shard_out is not None:
                    all_items.extend(shard_out.items)
            success_count = sum(1 for r in all_items if r.status == "success")
            failure_count = len(all_items) - success_count

            summary = {
                "batch_id": input.batch_id,
                "total_items": len(all_items),
                "success_count": success_count,
                "failure_count": failure_count,
                "shards": [
                    {
                        "shard_index": s.shard_index,
                        "success": sum(1 for r in s.items if r.status == "success"),
                        "failure": sum(1 for r in s.items if r.status == "failure"),
                    }
                    for s in shard_results if s is not None
                ],
            }
            per_item = [_per_item_row(r) for r in all_items]
            failures = [_failure_row(r) for r in all_items if r.status == "failure"]

            write_out = await workflow.execute_activity(
                write_report_activity,
                WriteReportInput(
                    report_root=input.report_root,
                    batch_id=input.batch_id,
                    summary=summary,
                    per_item=per_item,
                    failures=failures,
                ),
                task_queue=CPU_TASK_QUEUE,
                start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
                heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )

            # Stage 6: rich report — best-effort. Needs `region` for CloudWatch,
            # so it's skipped in the no-fleet mode.
            if manages_fleet:
                try:
                    await workflow.execute_activity(
                        build_report_activity,
                        BuildReportInput(
                            batch_id=input.batch_id,
                            region=input.region,
                            report_root=input.report_root,
                            pull_hardware=True,
                        ),
                        task_queue=CPU_TASK_QUEUE,
                        start_to_close_timeout=_BUILD_REPORT_TIMEOUT,
                        heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
                        retry_policy=DEFAULT_RETRY_POLICY,
                    )
                except ActivityError as exc:
                    workflow.logger.warning(
                        "build_report_activity failed (non-fatal — re-runnable via "
                        "`python -m prod.reports batch %s`): %s",
                        input.batch_id, exc,
                    )

            result = BatchRunOutput(
                batch_id=input.batch_id,
                total_items=len(all_items),
                success_count=success_count,
                failure_count=failure_count,
                report_uris=write_out.report_uris,
            )
        except asyncio.CancelledError:
            cancelled = True
        except Exception as exc:
            main_exc = exc

        # Teardown — runs even on cancellation. ABANDON so the workflow's
        # cancelled state doesn't cancel the activity itself; the await may
        # raise but the activity has already been scheduled and will execute
        # on a worker.
        if manages_fleet:
            try:
                teardown_out = await workflow.execute_activity(
                    scale_fleet_down_activity,
                    ScaleFleetDownInput(
                        region=input.region,
                        cpu_queue_asg_name=input.cpu_queue_asg_name,
                        gpu_queue_asg_name=input.gpu_queue_asg_name,
                    ),
                    task_queue=CPU_TASK_QUEUE,
                    start_to_close_timeout=_SCALE_DOWN_TIMEOUT,
                    heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
                    retry_policy=DEFAULT_RETRY_POLICY,
                    cancellation_type=workflow.ActivityCancellationType.ABANDON,
                )
                if not (teardown_out.cpu_ok and teardown_out.gpu_ok):
                    workflow.logger.warning(
                        "scale_fleet_down partial: cpu_ok=%s gpu_ok=%s cpu_err=%s gpu_err=%s",
                        teardown_out.cpu_ok, teardown_out.gpu_ok,
                        teardown_out.cpu_error, teardown_out.gpu_error,
                    )
            except Exception as exc:
                # ABANDON detached the activity — even if the await raised
                # (e.g. CancelledError because the workflow is cancelled),
                # the scale-down will still run on a worker.
                workflow.logger.warning(
                    "teardown await raised (activity is ABANDON-detached and runs regardless): %s",
                    exc,
                )

        if cancelled:
            raise asyncio.CancelledError
        if main_exc is not None:
            raise main_exc
        assert result is not None
        return result

    async def _fan_out_shards(
        self, batch_id: str, shards: list, merged_config: dict,
        shards_in_flight: int, pdfs_per_shard_in_flight: int,
    ) -> list[ShardOutput | None]:
        sem = asyncio.Semaphore(shards_in_flight)
        shard_results: list[ShardOutput | None] = [None] * len(shards)

        async def run_shard(shard_idx: int) -> None:
            child_id = shard_workflow_id(batch_id, shard_idx)
            async with sem:
                try:
                    out: ShardOutput = await workflow.execute_child_workflow(
                        ShardWorkflow.run,
                        ShardInput(
                            batch_id=batch_id,
                            shard_index=shard_idx,
                            items=shards[shard_idx],
                            pipeline_config=merged_config,
                            max_in_flight=pdfs_per_shard_in_flight,
                        ),
                        id=child_id,
                        task_queue=CPU_TASK_QUEUE,
                        execution_timeout=SHARD_WORKFLOW_EXECUTION_TIMEOUT,
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    )
                    shard_results[shard_idx] = out
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Shard-level failure is rare (shards swallow per-PDF
                    # failures). Mark every item in the shard as failed so the
                    # report stays complete — the per-PDF state at the moment
                    # the shard died is undefined; a rerun re-attempts them all.
                    workflow.logger.error(
                        "ShardWorkflow %s failed: %s: %s",
                        child_id, type(exc).__name__, exc,
                    )
                    err = _truncate(f"shard-level failure: {type(exc).__name__}: {exc}", 1500)
                    shard_results[shard_idx] = ShardOutput(
                        shard_index=shard_idx,
                        items=[
                            ItemResult(
                                document_id=item.document_id,
                                pdf_uri=item.pdf_uri,
                                status="failure",
                                workflow_id=child_id,
                                error=err,
                            )
                            for item in shards[shard_idx]
                        ],
                    )

        async with asyncio.TaskGroup() as tg:
            for i in range(len(shards)):
                tg.create_task(run_shard(i))
        return shard_results


def _merge_config(base: dict, overrides: dict | None) -> dict:
    """Shallow-deep merge: a top-level section in overrides replaces the
    same section in base; nested dicts are merged one level deep."""
    if not overrides:
        return base
    merged = {**base}
    for section, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(section), dict):
            merged[section] = {**merged[section], **value}
        else:
            merged[section] = value
    return merged


def _per_item_row(r: ItemResult) -> dict:
    return {
        "document_id": r.document_id,
        "pdf_uri": r.pdf_uri,
        "status": r.status,
        "workflow_id": r.workflow_id,
        "tree_path": r.tree_path or "",
        "node_count": r.node_count if r.node_count is not None else "",
        "total_pages": r.total_pages if r.total_pages is not None else "",
        "error": (r.error or "").replace("\n", " ")[:500],
    }


def _failure_row(r: ItemResult) -> dict:
    return {
        "document_id": r.document_id,
        "pdf_uri": r.pdf_uri,
        "workflow_id": r.workflow_id,
        "error": r.error or "",
    }


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit] + f"…[+{len(s) - limit} more chars]"
