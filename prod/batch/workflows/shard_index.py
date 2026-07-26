"""ShardIndexWorkflow — index ~50 tree.json documents by fanning out
IndexDocumentWorkflow children.

Parallel motif to ShardWorkflow, distinct workflow type because Temporal
child-workflow signatures aren't polymorphic. Same shape: bounded per-item
concurrency, one ItemResult per document (success or failure captured, never
raised), history stays under the event-count budget.

Failure handling mirrors ShardWorkflow: a per-document child that errors
permanently (after activity retries) is caught here and recorded as an
`IndexItemResult(status="failure")`. The shard itself only raises on
cancellation.
"""

import asyncio

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy

with workflow.unsafe.imports_passed_through():
    from prod.batch.planner import per_index_workflow_id
    from prod.live.workflows.index_document import IndexDocumentWorkflow
    from prod.live.workflows.models import (
        IndexDocumentWorkflowInput,
        IndexDocumentWorkflowOutput,
    )
    from shared.temporal.task_queues import (
        BATCH_CPU_TQ,
        WORKFLOW_EXECUTION_TIMEOUT,
    )

    from .models import IndexItemResult, ShardIndexInput, ShardIndexOutput


@workflow.defn
class ShardIndexWorkflow:
    """Index one shard by spawning per-document IndexDocumentWorkflow children."""

    @workflow.run
    async def run(self, input: ShardIndexInput) -> ShardIndexOutput:
        sem = asyncio.Semaphore(input.max_in_flight)
        results: list[IndexItemResult | None] = [None] * len(input.items)

        async def run_one(local_idx: int) -> None:
            item = input.items[local_idx]
            child_id = per_index_workflow_id(input.batch_id, item.document_id)
            async with sem:
                try:
                    out: IndexDocumentWorkflowOutput = await workflow.execute_child_workflow(
                        IndexDocumentWorkflow.run,
                        IndexDocumentWorkflowInput(
                            document_id=item.document_id,
                            run_id=str(workflow.uuid4()),
                            tree_uri=item.tree_uri,
                            config=input.pipeline_config,
                        ),
                        id=child_id,
                        # Batch: IndexDocumentWorkflow runs on BATCH_CPU_TQ; the
                        # child's activity scheduler picks up BATCH_GPU_TQ from
                        # workflow.info() for the embed leg.
                        task_queue=BATCH_CPU_TQ,
                        execution_timeout=WORKFLOW_EXECUTION_TIMEOUT,
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    )
                    results[local_idx] = IndexItemResult(
                        document_id=item.document_id,
                        tree_uri=item.tree_uri,
                        status="success",
                        workflow_id=child_id,
                        collection_name=out.collection_name,
                        chunk_count=out.chunk_count,
                        embedded_count=out.embedded_count,
                        indexed_count=out.indexed_count,
                        total_tokens=out.total_tokens,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    workflow.logger.warning(
                        "index child failed for %s: %s: %s",
                        item.document_id, type(exc).__name__, exc,
                    )
                    results[local_idx] = IndexItemResult(
                        document_id=item.document_id,
                        tree_uri=item.tree_uri,
                        status="failure",
                        workflow_id=child_id,
                        error=_truncate(f"{type(exc).__name__}: {exc}", 2000),
                    )

        async with asyncio.TaskGroup() as tg:
            for i in range(len(input.items)):
                tg.create_task(run_one(i))

        return ShardIndexOutput(
            shard_index=input.shard_index,
            items=[r for r in results if r is not None],
        )


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit] + f"…[+{len(s) - limit} more chars]"
