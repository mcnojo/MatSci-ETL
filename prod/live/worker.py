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
import signal

import click
import yaml
from temporalio.worker import Worker

from etl.pipeline.activities import activities
from prod.live.workflows.process_pdf import ProcessPdfWorkflow
from prod.shared_infra.task_queues import (
    CPU_TASK_QUEUE,
    GPU_TASK_QUEUE,
    WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
)
from shared.temporal_client import connect_temporal

log = logging.getLogger("worker")


async def run_worker(
    temporal_address: str,
    temporal_namespace: str,
    max_concurrent_cpu: int,
    max_concurrent_gpu: int,
):
    client = await connect_temporal(temporal_address, namespace=temporal_namespace)
    log.info(
        "Connected to Temporal at %s (namespace=%s)",
        temporal_address, temporal_namespace,
    )

    cpu_worker = Worker(
        client,
        task_queue=CPU_TASK_QUEUE,
        workflows=[ProcessPdfWorkflow],
        activities=activities,
        max_concurrent_activities=max_concurrent_cpu,
        graceful_shutdown_timeout=WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
    )
    gpu_worker = Worker(
        client,
        task_queue=GPU_TASK_QUEUE,
        activities=activities,
        max_concurrent_activities=max_concurrent_gpu,
        graceful_shutdown_timeout=WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
    )

    log.info(
        "Starting workers: cpu-task-queue (max_concurrent=%d), "
        "gpu-task-queue (max_concurrent=%d)",
        max_concurrent_cpu, max_concurrent_gpu,
    )

    shutdown_event = asyncio.Event()

    def _on_signal(signame: str) -> None:
        log.info("Received %s, initiating graceful worker shutdown", signame)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal, sig.name)

    async with asyncio.TaskGroup() as tg:
        cpu_task = tg.create_task(cpu_worker.run())
        gpu_task = tg.create_task(gpu_worker.run())

        async def _await_shutdown() -> None:
            await shutdown_event.wait()
            # Shutdown returns when the worker has fully drained or the
            # graceful_shutdown_timeout has elapsed. Running shutdowns in
            # parallel keeps total drain time bounded by the timeout, not
            # double it.
            await asyncio.gather(cpu_worker.shutdown(), gpu_worker.shutdown())

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
