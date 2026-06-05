"""BatchRunWorkflow — top-level parent for a batch run.

Flow:
  1. fetch_manifest_activity reads the manifest from S3 (or local).
  2. Verify the input batch_id matches the manifest's batch_id (typo guard).
  3. Apply manifest-level config_overrides on top of the pipeline config.
  4. planner.shard_manifest produces shards.
  5. For each shard, execute_child_workflow(ShardWorkflow) with bounded
     concurrency (asyncio.Semaphore) so we don't issue thousands of
     start_child_workflow calls into Temporal at once.
  6. Aggregate per-shard ItemResults into the batch report.
  7. write_report_activity emits summary.json, per_item.csv, failures.jsonl
     under s3://<report_root>/<batch_id>/report/.

History bound: shards × 3 events (start + complete + meta) ≈ 60 events for a
20-shard / 1000-PDF batch — well under the 50k event limit. continue_as_new
is therefore not required at the 1000-PDF operator ceiling.
"""

import asyncio

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from prod.batch.activities import (
        FetchManifestInput,
        WriteReportInput,
        fetch_manifest_activity,
        write_report_activity,
    )
    from prod.batch.planner import shard_manifest, shard_workflow_id
    from prod.shared_infra.task_queues import (
        CPU_ACTIVITY_TIMEOUT,
        CPU_HEARTBEAT_TIMEOUT,
        CPU_TASK_QUEUE,
        DEFAULT_RETRY_POLICY,
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


@workflow.defn
class BatchRunWorkflow:
    """Parent workflow that fans a manifest out to ShardWorkflow children."""

    @workflow.run
    async def run(self, input: BatchRunInput) -> BatchRunOutput:
        # Stage 1: fetch + validate the manifest
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

        # Layer manifest config_overrides on top of the resolved pipeline config
        merged_config = _merge_config(input.pipeline_config, manifest.config_overrides)

        # Stage 2: shard
        shards = shard_manifest(manifest, shard_size=input.shard_size)
        workflow.logger.info(
            "batch %s: %d items -> %d shards (shard_size=%d, shards_in_flight=%d)",
            input.batch_id, len(manifest.items), len(shards),
            input.shard_size, input.shards_in_flight,
        )

        # Stage 3: fan out shards with bounded concurrency
        sem = asyncio.Semaphore(input.shards_in_flight)
        shard_results: list[ShardOutput | None] = [None] * len(shards)

        async def run_shard(shard_idx: int) -> None:
            child_id = shard_workflow_id(input.batch_id, shard_idx)
            async with sem:
                try:
                    out: ShardOutput = await workflow.execute_child_workflow(
                        ShardWorkflow.run,
                        ShardInput(
                            batch_id=input.batch_id,
                            shard_index=shard_idx,
                            items=shards[shard_idx],
                            pipeline_config=merged_config,
                            max_in_flight=input.pdfs_per_shard_in_flight,
                        ),
                        id=child_id,
                        task_queue=CPU_TASK_QUEUE,
                        execution_timeout=SHARD_WORKFLOW_EXECUTION_TIMEOUT,
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    )
                    shard_results[shard_idx] = out
                except ChildWorkflowError as exc:
                    # A shard-level failure (rare — shards swallow per-PDF
                    # failures internally) is surfaced as failures for every
                    # item in that shard so the report stays complete.
                    workflow.logger.error(
                        "ShardWorkflow %s failed: %s", child_id, exc,
                    )
                    shard_results[shard_idx] = ShardOutput(
                        shard_index=shard_idx,
                        items=[
                            ItemResult(
                                document_id=item.document_id,
                                pdf_uri=item.pdf_uri,
                                status="failure",
                                workflow_id=child_id,
                                error=f"shard-level failure: {_truncate(str(exc), 1500)}",
                            )
                            for item in shards[shard_idx]
                        ],
                    )

        async with asyncio.TaskGroup() as tg:
            for i in range(len(shards)):
                tg.create_task(run_shard(i))

        # Stage 4: aggregate
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

        # Stage 5: write report
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

        return BatchRunOutput(
            batch_id=input.batch_id,
            total_items=len(all_items),
            success_count=success_count,
            failure_count=failure_count,
            report_uris=write_out.report_uris,
        )


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
