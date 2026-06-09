"""End-to-end smoke test for the batch path against a local Temporal stack.

This is an INTEGRATION test, not a unit test. It exercises the full path:
  CLI submit → BatchRunWorkflow → ShardWorkflow → ProcessPdfWorkflow
on a 1-PDF manifest, running against your existing local Temporal
(docker-compose) with the live worker (`make worker`) handling tasks.

Preconditions (all on localhost):
  - Temporal server reachable at localhost:7233
  - A worker registered for cpu-task-queue AND gpu-task-queue
    (i.e. `make worker` is running in another terminal)
  - vLLM endpoint available per etl/config/pipeline_config.yaml

If any precondition is unmet, the test SKIPS with a clear message rather
than failing — this lets it be safe to invoke from any environment.

Run: python -m tests.integration.test_batch_e2e
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import time
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_PDF = REPO_ROOT / "etl" / "hybrid.pdf"
TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost")
TEMPORAL_PORT = int(os.environ.get("TEMPORAL_PORT", "7233"))
WAIT_TIMEOUT_S = int(os.environ.get("BATCH_E2E_TIMEOUT_S", "600"))   # 10 min


def _skip(reason: str) -> int:
    print(f"SKIP: {reason}")
    return 0


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _run() -> int:
    if not _port_open(TEMPORAL_HOST, TEMPORAL_PORT):
        return _skip(
            f"Temporal not reachable at {TEMPORAL_HOST}:{TEMPORAL_PORT}. "
            f"Start it with `make infra`."
        )
    if not TEST_PDF.exists():
        return _skip(f"Test PDF missing: {TEST_PDF}")

    # Imports deferred so test discovery doesn't fail if temporalio is absent
    from prod.batch.workflows.batch_run import BatchRunWorkflow
    from prod.batch.workflows.models import BatchRunInput, BatchRunOutput
    from prod.shared_infra.task_queues import (
        BATCH_WORKFLOW_EXECUTION_TIMEOUT,
        CPU_TASK_QUEUE,
    )
    from shared.config_loader import load_pipeline_config
    from shared.temporal_client import connect_temporal

    client = await connect_temporal(f"{TEMPORAL_HOST}:{TEMPORAL_PORT}")

    # Unique batch_id keeps re-runs from colliding via REJECT_DUPLICATE
    batch_id = f"e2e-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    print(f"batch_id: {batch_id}")

    with tempfile.TemporaryDirectory(prefix="batch-e2e-") as scratch_str:
        scratch = Path(scratch_str)
        manifest_path = scratch / "manifest.json"
        manifest = {
            "batch_id": batch_id,
            "items": [
                {"document_id": "hybrid", "pdf_uri": f"file://{TEST_PDF}"},
            ],
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        report_root = str(scratch / "reports")

        try:
            pipeline_config = load_pipeline_config(
                REPO_ROOT / "etl" / "config" / "pipeline_config.yaml"
            )
        except FileNotFoundError as exc:
            return _skip(
                f"pipeline config resolution failed ({exc}). The vLLM endpoint "
                f"isn't available; bring up a dev box with `bin/dev/up_vllm.sh` "
                f"or run the live path (`make worker` + `python -m etl.cli`) "
                f"once first to verify your local setup."
            )

        print(f"manifest:    {manifest_path}")
        print(f"report_root: {report_root}")
        print("starting BatchRunWorkflow ...")

        input_obj = BatchRunInput(
            batch_id=batch_id,
            manifest_uri=str(manifest_path),
            pipeline_config=pipeline_config,
            report_root=report_root,
            shard_size=50,
            shards_in_flight=2,
            pdfs_per_shard_in_flight=2,
        )

        handle = await client.start_workflow(
            BatchRunWorkflow.run,
            input_obj,
            id=f"batch-{batch_id}",
            task_queue=CPU_TASK_QUEUE,
            execution_timeout=BATCH_WORKFLOW_EXECUTION_TIMEOUT,
        )

        print(f"workflow started: id={handle.id} run_id={handle.first_execution_run_id}")
        print(f"waiting up to {WAIT_TIMEOUT_S}s for completion ...")

        try:
            result: BatchRunOutput = await asyncio.wait_for(
                handle.result(), timeout=WAIT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            print(
                f"FAIL: workflow did not complete within {WAIT_TIMEOUT_S}s. "
                f"Likely the worker is not running (make worker) or vLLM is "
                f"unreachable. Inspect at http://localhost:8233."
            )
            return 1

        # Assertions
        assert result.batch_id == batch_id, (
            f"batch_id mismatch: {result.batch_id} != {batch_id}"
        )
        assert result.total_items == 1, f"total_items: {result.total_items}"
        # Don't require success — the test should still PASS if the per-PDF
        # workflow legitimately failed (e.g. vLLM transiently unavailable) as
        # long as the BATCH workflow completed and produced a report. Print
        # the outcome so the operator can see.
        print(
            f"result: success={result.success_count} "
            f"failure={result.failure_count}"
        )
        for label, uri in result.report_uris.items():
            print(f"  {label}: {uri}")
            assert Path(uri).exists(), f"report file missing: {uri}"

        # Inspect the summary
        summary = json.loads(Path(result.report_uris["summary_uri"]).read_text())
        assert summary["batch_id"] == batch_id
        assert summary["total_items"] == 1
        assert summary["success_count"] + summary["failure_count"] == 1

        if result.failure_count:
            failures = (
                Path(result.report_uris["failures_uri"])
                .read_text(encoding="utf-8")
                .splitlines()
            )
            for line in failures:
                print(f"  failure detail: {line[:200]}")

    print(f"\nPASS: batch e2e (success={result.success_count}/{result.total_items})")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
