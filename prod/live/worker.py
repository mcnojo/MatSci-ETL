"""Temporal worker — polls both cpu and gpu task queues.

Usage:
    python -m prod.live.worker
    python -m prod.live.worker --config prod/live/config/prod_config.yaml
    python -m prod.live.worker --temporal-address 10.0.1.5:7233

Graceful shutdown: on SIGTERM/SIGINT (e.g. Spot interruption notice, systemd
stop, Ctrl-C) we call `worker.shutdown()` on both workers. The
graceful_shutdown_timeout controls how long activities are given to complete
before they are cancelled. Activities that don't finish in time will be
retried by Temporal on a different worker via the heartbeat-timeout path.
"""

import asyncio
import logging
import os
import signal

import click
import yaml
from temporalio.worker import Worker

from etl.pipeline.activities import activities as etl_activities
from prod.batch.workflows.activities import activities as batch_activities
from prod.batch.workflows.batch_run import BatchRunWorkflow
from prod.batch.workflows.shard import ShardWorkflow
from prod.live.workflows.process_pdf import ProcessPdfWorkflow
from shared.temporal.task_queues import (
    CPU_TASK_QUEUE,
    GPU_TASK_QUEUE,
    WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
)
from shared.temporal.client import connect_temporal

# CPU queue runs orchestration workflows (process-pdf, shard, batch-run) and
# all CPU-bound activities (PyMuPDF, tree/asset construction, batch IO).
CPU_WORKFLOWS = [ProcessPdfWorkflow, ShardWorkflow, BatchRunWorkflow]
CPU_ACTIVITIES = etl_activities + batch_activities
# GPU queue runs only GPU activities (text LLM, Chandra OCR). It does not host
# workflows — they live on the CPU worker — so a Spot reclamation of a GPU
# instance only loses in-flight inference attempts, not orchestration state.
GPU_ACTIVITIES = etl_activities

log = logging.getLogger("worker")


VALID_QUEUES = {"cpu", "gpu"}


async def run_worker(
    temporal_address: str,
    temporal_namespace: str,
    queues: set[str],
    max_concurrent_cpu: int,
    max_concurrent_gpu: int,
):
    # Activities (await_pollers, build_report) need a Temporal client of their
    # own to drive DescribeTaskQueue / fetch_history_events. Export the
    # worker's own coords so they don't re-discover them.
    os.environ["TEMPORAL_ADDRESS"] = temporal_address
    os.environ["TEMPORAL_NAMESPACE"] = temporal_namespace

    client = await connect_temporal(temporal_address, namespace=temporal_namespace)
    log.info(
        "Connected to Temporal at %s (namespace=%s)",
        temporal_address, temporal_namespace,
    )

    workers: list[Worker] = []
    if "cpu" in queues:
        workers.append(Worker(
            client,
            task_queue=CPU_TASK_QUEUE,
            workflows=CPU_WORKFLOWS,
            activities=CPU_ACTIVITIES,
            max_concurrent_activities=max_concurrent_cpu,
            graceful_shutdown_timeout=WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
        ))
        log.info("Polling cpu-task-queue (max_concurrent=%d)", max_concurrent_cpu)
    if "gpu" in queues:
        workers.append(Worker(
            client,
            task_queue=GPU_TASK_QUEUE,
            activities=GPU_ACTIVITIES,
            max_concurrent_activities=max_concurrent_gpu,
            graceful_shutdown_timeout=WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
        ))
        log.info("Polling gpu-task-queue (max_concurrent=%d)", max_concurrent_gpu)

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
            # Parallel shutdown so total drain is bounded by graceful_shutdown_timeout, not N×.
            await asyncio.gather(*(w.shutdown() for w in workers))

        tg.create_task(_await_shutdown())


@click.command()
@click.option("--config", "config_path", default=None)
@click.option("--temporal-address", default="localhost:7233", show_default=True)
@click.option("--temporal-namespace", default="default", show_default=True)
@click.option(
    "--queues", "queues_str", default="cpu,gpu", show_default=True,
    help="Comma-separated task queues to poll: cpu, gpu, or both.",
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

    queues = {q.strip().lower() for q in queues_str.split(",") if q.strip()}
    invalid = queues - VALID_QUEUES
    if invalid:
        raise click.BadParameter(f"unknown queues: {sorted(invalid)} (valid: {sorted(VALID_QUEUES)})")
    if not queues:
        raise click.BadParameter("--queues must include at least one of: cpu, gpu")

    asyncio.run(run_worker(
        temporal_address=temporal_address,
        temporal_namespace=temporal_namespace,
        queues=queues,
        max_concurrent_cpu=max_concurrent_cpu,
        max_concurrent_gpu=max_concurrent_gpu,
    ))


if __name__ == "__main__":
    main()
