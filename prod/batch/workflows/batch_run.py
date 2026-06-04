"""BatchRunWorkflow — parent workflow that fans a manifest out to shards.

Phase 3 (current): module placeholder. Phase 4 implements:
  1. fetch_manifest_activity reads the manifest from S3.
  2. planner.shard_manifest produces shards.
  3. For each shard, start_child_workflow(ShardWorkflow, ...) with bounded
     concurrency (asyncio.Semaphore over child starts).
  4. Aggregate per-shard outcomes into a batch report.
  5. write_report_activity emits summary.json / per_item.csv / failures.jsonl.
  6. continue_as_new if shards > ~200 to stay under history limits.
"""

# Workflow class lands here in Phase 4.
