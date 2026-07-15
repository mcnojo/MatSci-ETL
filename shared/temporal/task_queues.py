"""Task queue names and shared retry/timeout constants.

Queue layout is motif-scoped (live vs batch) AND lane-scoped (control / cpu /
gpu). The split is the load-balancing barrier that keeps the always-on
cpu-pipeline-01 from siphoning work meant for ephemeral batch ASG instances —
which run different code (boot-time git clone) and different IAM/SG context.

  batch-control-tq  cpu-pipeline-01      orchestration only: BatchRunWorkflow
                                         body + scale_fleet + await_pollers
                                         + fetch_manifest + build_report.
                                         MUST be polled by an always-on worker
                                         because scale_fleet_up runs BEFORE
                                         the batch ASGs exist.
  batch-cpu-tq      batch CPU ASG        ShardWorkflow + per-PDF CPU activities.
  batch-gpu-tq      batch GPU ASG        LLM/Chandra calls during a batch.

  live-cpu-tq       cpu-pipeline-01      ProcessPdfWorkflow + per-PDF CPU.
  live-gpu-tq       cpu-pipeline-01      LLM/Chandra calls for live (HTTP-only
                                         from cpu-pipeline-01 to vLLM).
"""
from datetime import timedelta

from temporalio.common import RetryPolicy

BATCH_CONTROL_TQ = "batch-control-tq"
BATCH_CPU_TQ = "batch-cpu-tq"
BATCH_GPU_TQ = "batch-gpu-tq"
LIVE_CPU_TQ = "live-cpu-tq"
LIVE_GPU_TQ = "live-gpu-tq"

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

# start_to_close = per-attempt ceiling; heartbeat = liveness gap (kept tight
# for fast Spot-preempt detection; await_with_heartbeats ticks every 20s).

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
