"""Temporal worker — polls both cpu and gpu task queues.

Usage:
    python -m prod.worker
    python -m prod.worker --config prod/config/prod_config.yaml
    python -m prod.worker --temporal-address 10.0.1.5:7233
"""

import asyncio
import logging

import click
import yaml
from temporalio.worker import Worker

from etl.pipeline.activities import activities
from prod.task_queues import CPU_TASK_QUEUE, GPU_TASK_QUEUE
from prod.workflows.process_pdf import ProcessPdfWorkflow
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

    # Two workers on the same client, one per task queue
    cpu_worker = Worker(
        client,
        task_queue=CPU_TASK_QUEUE,
        workflows=[ProcessPdfWorkflow],
        activities=activities,
        max_concurrent_activities=max_concurrent_cpu,
    )
    gpu_worker = Worker(
        client,
        task_queue=GPU_TASK_QUEUE,
        activities=activities,
        max_concurrent_activities=max_concurrent_gpu,
    )

    log.info(
        "Starting workers: cpu-task-queue (max_concurrent=%d), "
        "gpu-task-queue (max_concurrent=%d)",
        max_concurrent_cpu, max_concurrent_gpu,
    )

    async with asyncio.TaskGroup() as tg:
        tg.create_task(cpu_worker.run())
        tg.create_task(gpu_worker.run())


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
