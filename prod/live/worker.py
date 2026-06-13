"""Live-motif Temporal worker — polls LIVE_CPU_TQ and LIVE_GPU_TQ.

Runs on cpu-pipeline-01 and on operator laptops. Registers only the live
workflow + per-PDF activities; batch lifecycle is owned by prod.batch.worker.

Usage:
    python -m prod.live.worker
    python -m prod.live.worker --config prod/live/config/prod_config.yaml
    python -m prod.live.worker --temporal-address 10.0.1.5:7233

Graceful shutdown: on SIGTERM/SIGINT (e.g. systemd stop, Ctrl-C) the worker
calls shutdown() on both inner workers. graceful_shutdown_timeout caps the
drain window before in-flight activities are cancelled and retried elsewhere.
"""

import asyncio
import logging
import os
import signal

import click
import yaml
from temporalio.worker import Worker

from etl.pipeline.activities import CPU_ACTIVITIES, GPU_ACTIVITIES
from prod.live.workflows.process_pdf import ProcessPdfWorkflow
from shared.temporal.task_queues import (
    LIVE_CPU_TQ,
    LIVE_GPU_TQ,
    WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
)
from shared.temporal.client import connect_temporal

log = logging.getLogger("worker")


async def run_worker(
    temporal_address: str,
    temporal_namespace: str,
    max_concurrent_cpu: int,
    max_concurrent_gpu: int,
):
    # build_report and similar activities need a Temporal client of their own;
    # export the worker's coords so they don't re-discover them.
    os.environ["TEMPORAL_ADDRESS"] = temporal_address
    os.environ["TEMPORAL_NAMESPACE"] = temporal_namespace

    client = await connect_temporal(temporal_address, namespace=temporal_namespace)
    log.info(
        "Connected to Temporal at %s (namespace=%s)",
        temporal_address, temporal_namespace,
    )

    workers = [
        Worker(
            client,
            task_queue=LIVE_CPU_TQ,
            workflows=[ProcessPdfWorkflow],
            activities=CPU_ACTIVITIES,
            max_concurrent_activities=max_concurrent_cpu,
            graceful_shutdown_timeout=WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
        ),
        Worker(
            client,
            task_queue=LIVE_GPU_TQ,
            activities=GPU_ACTIVITIES,
            max_concurrent_activities=max_concurrent_gpu,
            graceful_shutdown_timeout=WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
        ),
    ]
    log.info(
        "Polling %s (cpu_max=%d) and %s (gpu_max=%d)",
        LIVE_CPU_TQ, max_concurrent_cpu, LIVE_GPU_TQ, max_concurrent_gpu,
    )

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
            # Parallel shutdown so drain is bounded by graceful_shutdown_timeout, not N×.
            await asyncio.gather(*(w.shutdown() for w in workers))

        tg.create_task(_await_shutdown())


@click.command()
@click.option("--config", "config_path", default=None)
@click.option("--temporal-address", default="localhost:7233", show_default=True)
@click.option("--temporal-namespace", default="default", show_default=True)
@click.option("--max-concurrent-cpu", default=8, show_default=True)
@click.option("--max-concurrent-gpu", default=4, show_default=True)
def main(
    config_path: str | None,
    temporal_address: str,
    temporal_namespace: str,
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

    asyncio.run(run_worker(
        temporal_address=temporal_address,
        temporal_namespace=temporal_namespace,
        max_concurrent_cpu=max_concurrent_cpu,
        max_concurrent_gpu=max_concurrent_gpu,
    ))


if __name__ == "__main__":
    main()
