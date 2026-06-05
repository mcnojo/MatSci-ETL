"""Task queue names and shared retry/timeout constants.

cpu-task-queue: PyMuPDF, tree building, regex, formatting, S3 IO, finalize.
gpu-task-queue: text LLM (tree builder) + chandra OCR.

Phase 2 runs a single worker on both queues.
Phase 4 splits them across cpu-pipeline-01 and gpu-model-01.
"""
from datetime import timedelta

from temporalio.common import RetryPolicy

CPU_TASK_QUEUE = "cpu-task-queue"
GPU_TASK_QUEUE = "gpu-task-queue"

# Retry policies

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=3,  # 2s -> 6s -> 18s
)

GPU_RETRY_POLICY = RetryPolicy(
    maximum_attempts=5,
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2,
    maximum_interval=timedelta(seconds=30),
)

NO_RETRY_POLICY = RetryPolicy(maximum_attempts=1)

# Timeout defaults
# start_to_close: max wall-clock per single activity attempt.
# schedule_to_close: max wall-clock from task scheduled to final completion
#                    (spans all retry attempts).
# heartbeat:       max gap between activity.heartbeat() calls before Temporal
#                  considers the worker dead and schedules a retry. Set well
#                  below start_to_close so Spot interruptions are detected
#                  within seconds, not minutes.

CPU_ACTIVITY_TIMEOUT = timedelta(minutes=10)
GPU_ACTIVITY_TIMEOUT = timedelta(minutes=30)
CPU_HEARTBEAT_TIMEOUT = timedelta(seconds=30)
GPU_HEARTBEAT_TIMEOUT = timedelta(seconds=60)

# Workflow execution timeouts span all activity retries and child workflows.
WORKFLOW_EXECUTION_TIMEOUT = timedelta(hours=2)              # per-PDF
SHARD_WORKFLOW_EXECUTION_TIMEOUT = timedelta(hours=12)       # ~50 PDFs with bounded concurrency
BATCH_WORKFLOW_EXECUTION_TIMEOUT = timedelta(hours=24)       # parent of all shards

# Worker shutdown — Spot termination notice is 120s; leave headroom.
WORKER_GRACEFUL_SHUTDOWN_TIMEOUT = timedelta(seconds=90)
