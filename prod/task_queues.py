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
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2,  # 5s -> 10s -> 20s
)

NO_RETRY_POLICY = RetryPolicy(maximum_attempts=1)

# Timeout defaults
# start_to_close: max wall-clock per single activity attempt.
# schedule_to_close: max wall-clock from task scheduled to final completion
#                    (spans all retry attempts).

CPU_ACTIVITY_TIMEOUT = timedelta(minutes=10)
GPU_ACTIVITY_TIMEOUT = timedelta(minutes=30)
WORKFLOW_EXECUTION_TIMEOUT = timedelta(hours=2)
