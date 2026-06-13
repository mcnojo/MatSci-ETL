"""ShardWorkflow — process ~50 PDFs by fanning out ProcessPdfWorkflow children.

Why a separate workflow per shard rather than fanning out from the parent
BatchRunWorkflow directly: keeps any single workflow's event history bounded
and makes per-shard cancel/retry granular. Each ShardWorkflow's history
contains ~3 events per child (start + complete + signal) × ~50 children =
under 200 events. The parent BatchRunWorkflow's history scales with shard
count, not item count.

Failure handling: a per-PDF child workflow that errors permanently (after
its own retries) is caught here and recorded as an `ItemResult(status=
"failure")`. The shard itself never raises just because one PDF failed —
the operator needs the full report including failures.
"""

import asyncio

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy

with workflow.unsafe.imports_passed_through():
    from prod.batch.planner import per_pdf_workflow_id
    from prod.live.workflows.models import (
        ProcessPdfWorkflowInput,
        ProcessPdfWorkflowOutput,
    )
    from prod.live.workflows.process_pdf import ProcessPdfWorkflow
    from shared.temporal.task_queues import (
        BATCH_CPU_TQ,
        WORKFLOW_EXECUTION_TIMEOUT,
    )

    from .models import ItemResult, ShardInput, ShardOutput


@workflow.defn
class ShardWorkflow:
    """Process one shard of a batch by spawning per-PDF ProcessPdfWorkflow children."""

    @workflow.run
    async def run(self, input: ShardInput) -> ShardOutput:
        sem = asyncio.Semaphore(input.max_in_flight)
        results: list[ItemResult | None] = [None] * len(input.items)

        async def run_one(local_idx: int) -> None:
            item = input.items[local_idx]
            child_id = per_pdf_workflow_id(input.batch_id, item.document_id)
            async with sem:
                try:
                    out: ProcessPdfWorkflowOutput = await workflow.execute_child_workflow(
                        ProcessPdfWorkflow.run,
                        ProcessPdfWorkflowInput(
                            document_id=item.document_id,
                            run_id=str(workflow.uuid4()),
                            pdf_path=item.pdf_uri,
                            config=input.pipeline_config,
                        ),
                        id=child_id,
                        # Batch mode: ProcessPdfWorkflow runs on BATCH_CPU_TQ
                        # and its activity scheduler resolves the matching
                        # GPU sibling (BATCH_GPU_TQ) from workflow.info().
                        task_queue=BATCH_CPU_TQ,
                        execution_timeout=WORKFLOW_EXECUTION_TIMEOUT,
                        # USE_EXISTING: if a prior attempt of the same batch
                        # already started this PDF and it's still running,
                        # re-attach instead of double-running.
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    )
                    results[local_idx] = ItemResult(
                        document_id=item.document_id,
                        pdf_uri=item.pdf_uri,
                        status="success",
                        workflow_id=child_id,
                        tree_path=out.tree_path,
                        node_count=out.node_count,
                        total_pages=out.total_pages,
                        metrics_summary=out.metrics_summary,
                    )
                except asyncio.CancelledError:
                    # TaskGroup cancellation must propagate. Anything else
                    # (real child failure, sandbox violation, bad input) gets
                    # recorded so the shard finishes and the report shows it.
                    raise
                except Exception as exc:
                    workflow.logger.warning(
                        "child workflow failed for %s: %s: %s",
                        item.document_id, type(exc).__name__, exc,
                    )
                    results[local_idx] = ItemResult(
                        document_id=item.document_id,
                        pdf_uri=item.pdf_uri,
                        status="failure",
                        workflow_id=child_id,
                        error=_truncate(f"{type(exc).__name__}: {exc}", 2000),
                    )

        async with asyncio.TaskGroup() as tg:
            for i in range(len(input.items)):
                tg.create_task(run_one(i))

        return ShardOutput(
            shard_index=input.shard_index,
            items=[r for r in results if r is not None],
        )


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit] + f"…[+{len(s) - limit} more chars]"
