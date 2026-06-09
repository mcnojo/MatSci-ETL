"""SQS ingestion consumer — polls for PDF S3 URIs and starts workflows.

Tiny long-running daemon. Does no processing itself — it owns the SQS contract and the workflow ID format.

Usage:
    python -m prod.live.ingestion.consumer
    python -m prod.live.ingestion.consumer --config prod/live/config/prod_config.yaml

Flow:
    S3 bucket notification -> SQS pdf-ingestion-queue -> this consumer -> Temporal ProcessPdfWorkflow start -> SQS message deleted
"""

import asyncio
import logging
import os
import re
import sys
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import yaml
import boto3
from temporalio.client import Client

from prod.live.workflows.process_pdf import ProcessPdfWorkflow
from prod.live.workflows.models import ProcessPdfWorkflowInput
from prod.shared_infra.task_queues import CPU_TASK_QUEUE, WORKFLOW_EXECUTION_TIMEOUT
from shared.config_loader import load_pipeline_config
from shared.temporal_client import connect_temporal

log = logging.getLogger("ingestion_consumer")


def derive_document_id(s3_uri: str) -> str:
    """Derive a stable document_id from an S3 URI.

    s3://bucket/raw-pdfs/some-paper.pdf -> some-paper
    """
    parsed = urlparse(s3_uri)
    stem = PurePosixPath(parsed.path).stem
    # Sanitize: lowercase, replace non-alphanum with hyphens, collapse runs
    sanitized = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return sanitized or "unknown"


async def poll_loop(
    sqs_client,
    queue_url: str,
    temporal_client: Client,
    pipeline_config: dict,
    poll_interval_s: int,
    visibility_timeout_s: int,
):
    log.info("Polling %s (interval=%ds)", queue_url, poll_interval_s)

    while True:
        resp = sqs_client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=min(poll_interval_s, 20),  # long-poll capped at 20s
            VisibilityTimeout=visibility_timeout_s,
        )

        messages = resp.get("Messages", [])
        if not messages:
            await asyncio.sleep(max(0, poll_interval_s - 20))
            continue

        for msg in messages:
            s3_uri = msg["Body"].strip()
            if not s3_uri:
                log.warning("Empty message body, deleting")
                sqs_client.delete_message(
                    QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"],
                )
                continue

            document_id = derive_document_id(s3_uri)
            run_id = str(uuid.uuid4())
            workflow_id = f"process-pdf-{document_id}-{run_id}"

            log.info(
                "Starting workflow %s (doc=%s, uri=%s)",
                workflow_id, document_id, s3_uri,
            )

            try:
                await temporal_client.start_workflow(
                    ProcessPdfWorkflow.run,
                    ProcessPdfWorkflowInput(
                        document_id=document_id,
                        run_id=run_id,
                        pdf_path=s3_uri,
                        config=pipeline_config,
                    ),
                    id=workflow_id,
                    task_queue=CPU_TASK_QUEUE,
                    execution_timeout=WORKFLOW_EXECUTION_TIMEOUT,
                )
            except Exception:
                log.exception("Failed to start workflow %s", workflow_id)
                # Don't delete — let visibility timeout expire so it retries
                continue

            sqs_client.delete_message(
                QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"],
            )
            log.info("Workflow %s started, message deleted", workflow_id)


def _load_pipeline_config(prod_cfg: dict) -> dict:
    """Load the ETL pipeline config and apply prod overrides."""
    base_path = Path(__file__).resolve().parents[3] / "etl" / "config" / "pipeline_config.yaml"
    config = load_pipeline_config(base_path)

    overrides = prod_cfg.get("pipeline_overrides", {})
    for section, values in overrides.items():
        if section not in config:
            config[section] = {}
        if isinstance(values, dict):
            config[section].update({k: v for k, v in values.items() if v is not None})
        else:
            config[section] = values

    # Apply storage config from prod
    if "storage" in prod_cfg:
        config["storage"] = prod_cfg["storage"]

    return config


CONFIG_PATH = "prod/live/config/prod_config.yaml"


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    with open(CONFIG_PATH) as f:
        prod_cfg = yaml.safe_load(f)

    ingestion_cfg = prod_cfg.get("ingestion", {})
    # Env var wins — the systemd unit on cpu-pipeline-01 injects this from the SSM
    # parameter published by infra/terraform/live, so prod_config.yaml stays clean.
    queue_url = os.environ.get("OCR_LIVE_QUEUE_URL") or ingestion_cfg.get("queue_url")
    if not queue_url:
        log.error("queue_url unset — provide OCR_LIVE_QUEUE_URL or ingestion.queue_url in %s", CONFIG_PATH)
        sys.exit(1)

    poll_interval_s = ingestion_cfg.get("poll_interval_s", 5)
    visibility_timeout_s = ingestion_cfg.get("visibility_timeout_s", 300)

    temporal_cfg = prod_cfg.get("temporal", {})
    temporal_address = temporal_cfg.get("address", "localhost:7233")
    temporal_namespace = temporal_cfg.get("namespace", "default")

    pipeline_config = _load_pipeline_config(prod_cfg)

    sqs_client = boto3.client(
        "sqs",
        region_name=prod_cfg.get("storage", {}).get("s3", {}).get("region"),
    )

    async def run():
        temporal_client = await connect_temporal(
            temporal_address, namespace=temporal_namespace,
        )
        log.info("Connected to Temporal at %s", temporal_address)
        await poll_loop(
            sqs_client, queue_url, temporal_client,
            pipeline_config, poll_interval_s, visibility_timeout_s,
        )

    asyncio.run(run())


if __name__ == "__main__":
    main()
