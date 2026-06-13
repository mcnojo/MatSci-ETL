"""Batch-motif Temporal worker — polls BATCH_{CONTROL,CPU,GPU}_TQ.

Three lanes, selected by --queues:
  control  BatchRunWorkflow body + lifecycle activities (fetch_manifest,
           scale_fleet_up/down, await_pollers, write_report, build_report).
           Polled by an always-on worker on cpu-pipeline-01 — scale_fleet_up
           must execute BEFORE the batch ASGs exist.
  cpu      ShardWorkflow + ProcessPdfWorkflow + per-PDF CPU activities.
           Polled by the batch CPU ASG only.
  gpu      Per-PDF GPU activities (LLM + Chandra OCR).
           Polled by the batch GPU ASG only.

Production usage (one lane per host):
    python -m prod.batch.worker --queues control     # on cpu-pipeline-01
    python -m prod.batch.worker --queues cpu         # on batch CPU ASG
    python -m prod.batch.worker --queues gpu         # on batch GPU ASG

Local testing — run all three lanes in one process:
    python -m prod.batch.worker --queues control,cpu,gpu
"""

import asyncio
import logging
import os
import signal

import click
import yaml
from temporalio.worker import Worker

from etl.pipeline.activities import CPU_ACTIVITIES, GPU_ACTIVITIES
from prod.batch.workflows.activities import activities as batch_activities
from prod.batch.workflows.batch_run import BatchRunWorkflow
from prod.batch.workflows.shard import ShardWorkflow
from prod.live.workflows.process_pdf import ProcessPdfWorkflow
from shared.temporal.task_queues import (
    BATCH_CONTROL_TQ,
    BATCH_CPU_TQ,
    BATCH_GPU_TQ,
    WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
)
from shared.temporal.client import connect_temporal

log = logging.getLogger("batch-worker")


VALID_LANES = {"control", "cpu", "gpu"}


def _build_workers(
    client, lanes: set[str],
    max_concurrent_cpu: int, max_concurrent_gpu: int,
) -> list[Worker]:
    workers: list[Worker] = []
    if "control" in lanes:
        workers.append(Worker(
            client,
            task_queue=BATCH_CONTROL_TQ,
            workflows=[BatchRunWorkflow],
            activities=batch_activities,
            # Control activities are lightweight (boto3 + S3); CPU cap is fine.
            max_concurrent_activities=max_concurrent_cpu,
            graceful_shutdown_timeout=WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
        ))
        log.info("Polling %s (max=%d)", BATCH_CONTROL_TQ, max_concurrent_cpu)
    if "cpu" in lanes:
        workers.append(Worker(
            client,
            task_queue=BATCH_CPU_TQ,
            workflows=[ShardWorkflow, ProcessPdfWorkflow],
            activities=CPU_ACTIVITIES,
            max_concurrent_activities=max_concurrent_cpu,
            graceful_shutdown_timeout=WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
        ))
        log.info("Polling %s (max=%d)", BATCH_CPU_TQ, max_concurrent_cpu)
    if "gpu" in lanes:
        workers.append(Worker(
            client,
            task_queue=BATCH_GPU_TQ,
            activities=GPU_ACTIVITIES,
            max_concurrent_activities=max_concurrent_gpu,
            graceful_shutdown_timeout=WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
        ))
        log.info("Polling %s (max=%d)", BATCH_GPU_TQ, max_concurrent_gpu)
    return workers


async def run_worker(
    temporal_address: str,
    temporal_namespace: str,
    lanes: set[str],
    max_concurrent_cpu: int,
    max_concurrent_gpu: int,
):
    os.environ["TEMPORAL_ADDRESS"] = temporal_address
    os.environ["TEMPORAL_NAMESPACE"] = temporal_namespace

    client = await connect_temporal(temporal_address, namespace=temporal_namespace)
    log.info(
        "Connected to Temporal at %s (namespace=%s); lanes=%s",
        temporal_address, temporal_namespace, sorted(lanes),
    )

    workers = _build_workers(client, lanes, max_concurrent_cpu, max_concurrent_gpu)

    shutdown_event = asyncio.Event()

    def _on_signal(signame: str) -> None:
        log.info("Received %s, initiating graceful worker shutdown", signame)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal, sig.name)

    async with asyncio.TaskGroup() as tg:
        for w in workers:
            tg.create_task(w.run())

        async def _await_shutdown() -> None:
            await shutdown_event.wait()
            await asyncio.gather(*(w.shutdown() for w in workers))

        tg.create_task(_await_shutdown())


@click.command()
@click.option("--config", "config_path", default=None)
@click.option("--temporal-address", default="localhost:7233", show_default=True)
@click.option("--temporal-namespace", default="default", show_default=True)
@click.option(
    "--queues", "queues_str", required=True,
    help="Comma-separated batch lanes to poll: control, cpu, gpu.",
)
@click.option("--max-concurrent-cpu", default=8, show_default=True)
@click.option("--max-concurrent-gpu", default=4, show_default=True)
def main(
    config_path: str | None,
    temporal_address: str,
    temporal_namespace: str,
    queues_str: str,
    max_concurrent_cpu: int,
    max_concurrent_gpu: int,
):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpcore", "httpx", "temporalio.worker", "temporalio.service"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if config_path:
        with open(config_path) as f:
            prod_cfg = yaml.safe_load(f)
        temporal_address = prod_cfg.get("temporal", {}).get("address", temporal_address)
        temporal_namespace = prod_cfg.get("temporal", {}).get("namespace", temporal_namespace)
        max_concurrent_cpu = prod_cfg.get("worker", {}).get("max_concurrent_cpu", max_concurrent_cpu)
        max_concurrent_gpu = prod_cfg.get("worker", {}).get("max_concurrent_gpu", max_concurrent_gpu)

    lanes = {q.strip().lower() for q in queues_str.split(",") if q.strip()}
    invalid = lanes - VALID_LANES
    if invalid:
        raise click.BadParameter(f"unknown lanes: {sorted(invalid)} (valid: {sorted(VALID_LANES)})")
    if not lanes:
        raise click.BadParameter(f"--queues must include at least one of: {sorted(VALID_LANES)}")

    asyncio.run(run_worker(
        temporal_address=temporal_address,
        temporal_namespace=temporal_namespace,
        lanes=lanes,
        max_concurrent_cpu=max_concurrent_cpu,
        max_concurrent_gpu=max_concurrent_gpu,
    ))


if __name__ == "__main__":
    main()
