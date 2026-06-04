"""ShardWorkflow — child workflow that processes ~50 items via ProcessPdfWorkflow.

Phase 3 (current): module placeholder. Phase 4 implements:
  - Iterate the shard's BatchItem list.
  - For each, start_child_workflow(ProcessPdfWorkflow, ...) with bounded
    concurrency via asyncio.Semaphore.
  - Collect per-item outcomes (success / failure + tree URI) and return
    them to BatchRunWorkflow.
"""

# Workflow class lands here in Phase 4.
