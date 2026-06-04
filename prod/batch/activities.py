"""Temporal activities for the batch path.

Phase 3 (current): empty registry. Phase 4 adds:
  - fetch_manifest_activity  — reads s3://.../manifest.json
  - write_report_activity    — writes summary.json, per_item.csv, failures.jsonl

Stage-level batch activities run on the existing cpu-task-queue alongside
the live activities (etl.pipeline.activities); both workers register both
lists.
"""

# Activity functions and their registry land here in Phase 4.
activities: list = []
