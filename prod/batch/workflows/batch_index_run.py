"""BatchIndexRunWorkflow — top-level parent for an indexing batch run.

Peer of BatchRunWorkflow. Owns the entire lifecycle for chunking + embedding
+ indexing a corpus of already-finalized trees. A single `cli submit-index`
is sufficient — no CLI babysitting.

Flow (when input.manages_fleet):
  1. fetch_index_manifest_activity reads the manifest and verifies its
     batch_id matches the input.
  2. scale_fleet_up_activity drives both batch ASGs to their target counts.
  3. await_pollers_activity blocks until activity pollers register on
     BATCH_CPU_TQ and BATCH_GPU_TQ.
  4. ShardIndexWorkflow children fan out with bounded concurrency, each
     spawning IndexDocumentWorkflow per document.
  5. write_report_activity emits summary.json + per_item.csv + failures.jsonl.
  6. finally: scale_fleet_down_activity zeroes both ASGs (ABANDON-detached
     so cancellation doesn't leave the fleet running).

When input does not specify a fleet (local dev, `--no-manage-fleet`), steps
2/3/6 are skipped. The rich Temporal+CloudWatch report from BatchRunWorkflow
is intentionally NOT run here — prod.reports.builder is coupled to the
process-PDF workflow shape and would need an index-specific analog to be
meaningful for this route. Flat report is sufficient for now.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from prod.batch.planner import index_shard_workflow_id, shard_index_manifest
    from prod.batch.workflows.activities.await_pollers import (
        AwaitPollersInput,
        await_pollers_activity,
    )
    from prod.batch.workflows.activities.fetch_index_manifest import (
        FetchIndexManifestInput,
        fetch_index_manifest_activity,
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
        BATCH_CONTROL_TQ,
        BATCH_CPU_TQ,
        BATCH_GPU_TQ,
        CPU_ACTIVITY_TIMEOUT,
        CPU_HEARTBEAT_TIMEOUT,
        DEFAULT_RETRY_POLICY,
        NO_RETRY_POLICY,
        SHARD_WORKFLOW_EXECUTION_TIMEOUT,
    )

    from .models import (
        BatchIndexRunInput,
        BatchIndexRunOutput,
        IndexBatchItem,
        IndexItemResult,
        ShardIndexInput,
        ShardIndexOutput,
    )
    from .shard_index import ShardIndexWorkflow


_SCALE_UP_TIMEOUT = timedelta(minutes=2)
_SCALE_DOWN_TIMEOUT = timedelta(minutes=15)
_AWAIT_POLLERS_BUFFER = timedelta(minutes=2)


@workflow.defn
class BatchIndexRunWorkflow:
    """Fan a tree manifest out to ShardIndexWorkflow children and own the
    batch fleet's lifecycle around the fan-out."""

    @workflow.run
    async def run(self, input: BatchIndexRunInput) -> BatchIndexRunOutput:
        fetch_out = await workflow.execute_activity(
            fetch_index_manifest_activity,
            FetchIndexManifestInput(manifest_uri=input.manifest_uri),
            task_queue=BATCH_CONTROL_TQ,
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
        shards = shard_index_manifest(manifest, shard_size=input.shard_size)
        workflow.logger.info(
            "index-batch %s: %d items -> %d shards (shard_size=%d, "
            "shards_in_flight=%d, manages_fleet=%s)",
            input.batch_id, len(manifest.items), len(shards),
            input.shard_size, input.shards_in_flight, input.manages_fleet,
        )

        manages_fleet = input.manages_fleet
        cancelled = False
        main_exc: BaseException | None = None
        result: BatchIndexRunOutput | None = None

        try:
            if manages_fleet:
                await workflow.execute_activity(
                    scale_fleet_up_activity,
                    ScaleFleetUpInput(
                        region=input.region,
                        cpu_queue_asg_name=input.cpu_queue_asg_name,
                        cpu_queue_desired=input.cpu_queue_desired,
                        gpu_queue_asg_name=input.gpu_queue_asg_name,
                        gpu_queue_desired=input.gpu_queue_desired,
                    ),
                    task_queue=BATCH_CONTROL_TQ,
                    start_to_close_timeout=_SCALE_UP_TIMEOUT,
                    heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
                    retry_policy=DEFAULT_RETRY_POLICY,
                )

                await workflow.execute_activity(
                    await_pollers_activity,
                    AwaitPollersInput(
                        namespace=workflow.info().namespace,
                        task_queues=[BATCH_CPU_TQ, BATCH_GPU_TQ],
                        timeout_s=input.worker_registration_timeout_s,
                    ),
                    task_queue=BATCH_CONTROL_TQ,
                    start_to_close_timeout=(
                        timedelta(seconds=input.worker_registration_timeout_s) + _AWAIT_POLLERS_BUFFER
                    ),
                    heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
                    retry_policy=NO_RETRY_POLICY,
                )

            shard_results = await self._fan_out_shards(
                input.batch_id, shards, merged_config,
                input.shards_in_flight, input.documents_per_shard_in_flight,
            )

            all_items: list[IndexItemResult] = []
            for shard_out in shard_results:
                if shard_out is not None:
                    all_items.extend(shard_out.items)
            success_count = sum(1 for r in all_items if r.status == "success")
            failure_count = len(all_items) - success_count

            summary = {
                "batch_id": input.batch_id,
                "route": "index",
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
                task_queue=BATCH_CONTROL_TQ,
                start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
                heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )

            result = BatchIndexRunOutput(
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

        if manages_fleet:
            try:
                teardown_out = await workflow.execute_activity(
                    scale_fleet_down_activity,
                    ScaleFleetDownInput(
                        region=input.region,
                        cpu_queue_asg_name=input.cpu_queue_asg_name,
                        gpu_queue_asg_name=input.gpu_queue_asg_name,
                    ),
                    task_queue=BATCH_CONTROL_TQ,
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
        shards_in_flight: int, documents_per_shard_in_flight: int,
    ) -> list[ShardIndexOutput | None]:
        sem = asyncio.Semaphore(shards_in_flight)
        shard_results: list[ShardIndexOutput | None] = [None] * len(shards)

        async def run_shard(shard_idx: int) -> None:
            child_id = index_shard_workflow_id(batch_id, shard_idx)
            async with sem:
                try:
                    items = [
                        IndexBatchItem(
                            document_id=item.document_id,
                            tree_uri=item.tree_uri,
                        )
                        for item in shards[shard_idx]
                    ]
                    out: ShardIndexOutput = await workflow.execute_child_workflow(
                        ShardIndexWorkflow.run,
                        ShardIndexInput(
                            batch_id=batch_id,
                            shard_index=shard_idx,
                            items=items,
                            pipeline_config=merged_config,
                            max_in_flight=documents_per_shard_in_flight,
                        ),
                        id=child_id,
                        task_queue=BATCH_CPU_TQ,
                        execution_timeout=SHARD_WORKFLOW_EXECUTION_TIMEOUT,
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    )
                    shard_results[shard_idx] = out
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    workflow.logger.error(
                        "ShardIndexWorkflow %s failed: %s: %s",
                        child_id, type(exc).__name__, exc,
                    )
                    err = _truncate(f"shard-level failure: {type(exc).__name__}: {exc}", 1500)
                    shard_results[shard_idx] = ShardIndexOutput(
                        shard_index=shard_idx,
                        items=[
                            IndexItemResult(
                                document_id=item.document_id,
                                tree_uri=item.tree_uri,
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
    """Shallow-deep merge — top-level sections replace; nested dicts merge one level.
    Mirrors BatchRunWorkflow._merge_config."""
    if not overrides:
        return base
    merged = {**base}
    for section, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(section), dict):
            merged[section] = {**merged[section], **value}
        else:
            merged[section] = value
    return merged


def _per_item_row(r: IndexItemResult) -> dict:
    return {
        "document_id": r.document_id,
        "tree_uri": r.tree_uri,
        "status": r.status,
        "workflow_id": r.workflow_id,
        "index_name": r.index_name or "",
        "chunk_count": r.chunk_count if r.chunk_count is not None else "",
        "embedded_count": r.embedded_count if r.embedded_count is not None else "",
        "indexed_count": r.indexed_count if r.indexed_count is not None else "",
        "total_tokens": r.total_tokens if r.total_tokens is not None else "",
        "error": (r.error or "").replace("\n", " ")[:500],
    }


def _failure_row(r: IndexItemResult) -> dict:
    return {
        "document_id": r.document_id,
        "tree_uri": r.tree_uri,
        "workflow_id": r.workflow_id,
        "error": r.error or "",
    }


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit] + f"…[+{len(s) - limit} more chars]"
