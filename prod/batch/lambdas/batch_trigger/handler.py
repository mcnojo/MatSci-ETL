"""S3 → BatchRunWorkflow trigger Lambda.

Fires on `<bucket>/<incoming_prefix>/<batch_id>/manifest.json`. Starts a
BatchRunWorkflow keyed by `batch_id` with REJECT_DUPLICATE — re-uploads are
idempotent no-ops. The workflow is referenced by string name so this bundle
imports no workflow / activity code (see build.sh for bundled assets).
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import unquote

import boto3
import yaml
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from prod.batch.models import BatchManifest
from prod.batch.planner import batch_workflow_id
from prod.batch.workflows.models import BatchRunInput
from shared.temporal.task_queues import (
    BATCH_WORKFLOW_EXECUTION_TIMEOUT,
    CPU_TASK_QUEUE,
)
from shared.temporal.client import connect_temporal


logger = logging.getLogger()
logger.setLevel(logging.INFO)

_HERE = Path(__file__).parent
_S3 = boto3.client("s3")


def _shallow_deep_merge(base: dict, overrides: dict) -> dict:
    # Mirrors BatchRunWorkflow._merge_config; duplicated to keep this Lambda workflow-import-free.
    if not overrides:
        return base
    merged = {**base}
    for section, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(section), dict):
            merged[section] = {**merged[section], **value}
        else:
            merged[section] = value
    return merged


def _load_pipeline_config() -> dict:
    # yaml.safe_load only — load_pipeline_config would resolve relative paths against
    # /var/task instead of the worker box; prod_config supplies the absolute paths.
    base = yaml.safe_load((_HERE / "pipeline_config.yaml").read_text()) or {}
    prod = yaml.safe_load((_HERE / "prod_config.yaml").read_text()) or {}
    overrides = prod.get("pipeline_overrides", {}) or {}
    return _shallow_deep_merge(base, overrides)


# Cold-start cached — config is immutable for the Lambda's lifetime.
_PIPELINE_CONFIG = _load_pipeline_config()


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"required env var {name} not set")
    return val


def _env_int(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        if default is None:
            raise RuntimeError(f"required env var {name} not set")
        return default
    return int(raw)


def handler(event: dict, context) -> dict:
    return asyncio.run(_handle(event))


async def _handle(event: dict) -> dict:
    incoming_prefix = _env("INCOMING_PREFIX")
    started: list[dict] = []
    skipped: list[dict] = []
    already_running: list[dict] = []

    client = None  # connect lazily — skip the cost if every record is filtered out

    for record in event.get("Records", []):
        s3_info = record.get("s3") or {}
        bucket = (s3_info.get("bucket") or {}).get("name")
        key = unquote((s3_info.get("object") or {}).get("key", ""))

        if not bucket or not key:
            logger.warning("skipping record without bucket/key: %s", json.dumps(record))
            continue

        # Defense-in-depth: S3 notification filters already enforce these.
        if not key.endswith("/manifest.json"):
            logger.info("skipping non-manifest key: s3://%s/%s", bucket, key)
            skipped.append({"bucket": bucket, "key": key, "reason": "not-manifest"})
            continue
        if not key.startswith(incoming_prefix):
            logger.info("skipping key outside incoming prefix: s3://%s/%s", bucket, key)
            skipped.append({"bucket": bucket, "key": key, "reason": "outside-prefix"})
            continue

        # Layout: <incoming_prefix><batch_id>/manifest.json
        rel = key[len(incoming_prefix):]
        parts = rel.split("/")
        if len(parts) != 2 or parts[1] != "manifest.json" or not parts[0]:
            logger.warning("skipping malformed manifest key: s3://%s/%s", bucket, key)
            skipped.append({"bucket": bucket, "key": key, "reason": "bad-layout"})
            continue
        batch_id_from_key = parts[0]

        body = _S3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        manifest = BatchManifest.model_validate_json(body)
        if manifest.batch_id != batch_id_from_key:
            # Refuse — guessing hides the uploader bug.
            raise ValueError(
                f"batch_id mismatch — manifest body has {manifest.batch_id!r} but "
                f"S3 key path implies {batch_id_from_key!r}. Refusing to start.",
            )

        manifest_uri = f"s3://{bucket}/{key}"
        input_obj = BatchRunInput(
            batch_id=manifest.batch_id,
            manifest_uri=manifest_uri,
            pipeline_config=_PIPELINE_CONFIG,
            report_root=_env("REPORT_ROOT"),
            shard_size=_env_int("SHARD_SIZE", 50),
            shards_in_flight=_env_int("SHARDS_IN_FLIGHT", 8),
            pdfs_per_shard_in_flight=_env_int("PDFS_PER_SHARD_IN_FLIGHT", 8),
            region=_env("FLEET_REGION"),
            cpu_queue_asg_name=_env("CPU_QUEUE_ASG_NAME"),
            gpu_queue_asg_name=_env("GPU_QUEUE_ASG_NAME"),
            cpu_queue_desired=_env_int("CPU_QUEUE_DESIRED"),
            gpu_queue_desired=_env_int("GPU_QUEUE_DESIRED"),
            worker_registration_timeout_s=_env_int("WORKER_REGISTRATION_TIMEOUT_S", 600),
        )

        if client is None:
            client = await connect_temporal(
                _env("TEMPORAL_ADDRESS"),
                namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
            )

        wf_id = batch_workflow_id(manifest.batch_id)
        try:
            await client.start_workflow(
                "BatchRunWorkflow",     # by-name — no workflow-class import in the bundle
                input_obj,
                id=wf_id,
                task_queue=CPU_TASK_QUEUE,
                execution_timeout=BATCH_WORKFLOW_EXECUTION_TIMEOUT,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            logger.info(
                "started workflow: id=%s manifest=%s items=%d",
                wf_id, manifest_uri, len(manifest.items),
            )
            started.append({
                "batch_id": manifest.batch_id, "workflow_id": wf_id,
                "items": len(manifest.items),
            })
        except WorkflowAlreadyStartedError:
            logger.info("workflow already running (idempotent no-op): id=%s", wf_id)
            already_running.append({"batch_id": manifest.batch_id, "workflow_id": wf_id})

    return {"started": started, "already_running": already_running, "skipped": skipped}
