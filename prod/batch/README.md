# prod/batch/ — bounded bulk-job processor

Counterpart to `prod/live/`. Optimizes for GPU utilization across a corpus
(saturate the fleet, then tear it down) rather than per-PDF latency.

## Layout

```
prod/batch/
├── cli.py             # operator entry: submit / status / cancel / wait-for-workers
├── worker.py          # Temporal worker; --queues control,cpu,gpu (one lane per host)
├── planner.py         # manifest -> shards (pure); S3 manifest URI convention
├── artifacts.py       # S3 manifest read
├── models.py          # BatchManifest, BatchItem
├── config/batch_config.yaml
├── terraform/         # batch ASGs (CPU + GPU) + IAM + CloudWatch; see its README
└── workflows/
    ├── batch_run.py   # BatchRunWorkflow (parent): scale up -> fan out -> report -> scale down
    ├── shard.py       # ShardWorkflow (child): ~50 PDFs via ProcessPdfWorkflow children
    ├── models.py      # BatchRunInput/Output, ShardInput/Output, ItemResult
    └── activities/    # fetch_manifest, scale_fleet, await_pollers, write_report, build_report
```

Per-PDF processing reuses `prod/live/workflows/process_pdf.py` (shared between
motifs); the CPU task queue it lands on selects the GPU sibling.

## Task queues

`batch-control-tq` (parent + lifecycle activities, polled by an always-on
worker on cpu-pipeline-01), `batch-cpu-tq` (shard + per-PDF CPU activities,
batch CPU ASG), `batch-gpu-tq` (LLM + Chandra OCR, batch GPU ASG).

## Workflow IDs

- Parent: `batch-{batch_id}`
- Shard:  `batch-{batch_id}-shard-{NNNN}`
- Per-PDF: `batch-{batch_id}-pdf-{document_id}`

## Manifest

```json
{
  "batch_id": "2026-q2-corpus-a",
  "items": [
    { "document_id": "j-acs-2024-001", "pdf_uri": "s3://chem-lit/raw/acs-2024-001.pdf" }
  ],
  "config_overrides": { "tree_llm": { "model": "..." } }
}
```

S3 layout (single source of truth in `planner.py`):

```
s3://<artifact_bucket>/batches/incoming/<batch_id>/manifest.json
s3://<artifact_bucket>/batches/incoming/<batch_id>/pdfs/<document_id>.pdf
```

## CLI

```bash
# Upload PDFs + manifest; prints batch_id and the submit command.
bin/batch/submit.sh <folder>

# Start the workflow (waits for completion; Ctrl-C cancels and tears down).
python -m prod.batch.cli submit <batch_id>

# Run against a pre-existing fleet (local dev / debug).
python -m prod.batch.cli submit <batch_id> --no-manage-fleet

python -m prod.batch.cli status <batch_id>
python -m prod.batch.cli cancel <batch_id>
python -m prod.batch.cli wait-for-workers --queues cpu,gpu
```

`submit` defaults `--prod-overlay` to `prod/live/config/prod_config.yaml` —
the same overlay live uses, so both motifs write trees to
`s3://chem-lit-artifacts/trees/<document_id>/tree.json` (via
`output.kb_root` in the base pipeline config). Pass `--prod-overlay ''`
to skip the scale-run knobs.

Fleet wiring (region, ASG names, scale targets, registration timeout) and
the artifact bucket are read from `prod/batch/terraform` outputs at submit
time — terraform is the single source of truth.

There is no S3 -> Lambda auto-trigger. Submission is explicit so teardown
stays fast (no Lambda VPC ENIs to wait on during `down.sh`).

## Artifacts

- Per-PDF tree: `s3://chem-lit-artifacts/trees/<document_id>/tree.json`
  (`kb_root` from the shared overlay; same prefix for live and batch).
- Batch report: `{report.s3_root}/batches/<batch_id>/report/report.{json,md}`
  per `batch_config.yaml`.
- `bin/pull-trees.sh [dest]` syncs `s3://<bucket>/trees/` to local disk;
  works for both motifs.

Reports CLI (cross-motif analytics) lives at `python -m prod.reports`.

## Testing

```bash
# Unit (no infra):
python -m tests.test_batch_planner       # sharding, ID format, validation
python -m tests.test_batch_workflows     # pure helpers

# Integration smoke (needs a reachable Temporal + worker + vLLM):
python -m tests.integration.test_batch_e2e
```

The integration test submits a 1-PDF manifest pointing at `etl/hybrid.pdf`
and runs `BatchRunWorkflow -> ShardWorkflow -> ProcessPdfWorkflow` with
fleet management off. Skips politely if Temporal or vLLM is unavailable.
