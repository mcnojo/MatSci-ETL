# prod/batch/ — bounded bulk-job processor

Counterpart to `prod/live/`. Optimizes for **GPU utilization across a full
corpus** (saturate the fleet, then tear it down) rather than per-PDF
latency.

## Layout

```
prod/batch/
├── cli.py                 # operator entry: run / submit / status / cancel / report / teardown-fleet
├── planner.py             # manifest → shards (pure)
├── artifacts.py           # S3 manifest read, report write
├── models.py              # BatchManifest, BatchItem
├── activities.py          # fetch_manifest_activity, write_report_activity
├── reports/               # end-of-batch hardware + workflow report
├── workflows/
│   ├── batch_run.py       # BatchRunWorkflow (parent — fan out + report)
│   └── shard.py           # ShardWorkflow (child — ~50 PDFs)
├── config/
│   └── batch_config.yaml
└── scripts/
    └── user_data.sh.tpl   # worker bootstrap (rendered by terraform)
```

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

`document_id` is operator-supplied (reproducible across re-runs).

## CLI

```bash
# Full lifecycle: scale fleet, submit, wait, build report, scale down.
python -m prod.batch.cli run --manifest s3://chem-lit-artifacts/batches/q2-corpus-a/manifest.json

# Submit against an already-running fleet (no scale up/down).
python -m prod.batch.cli submit --manifest /path/to/manifest.json --wait
python -m prod.batch.cli submit --manifest /path/to/manifest.json --dry-run

# Inspect
python -m prod.batch.cli status <batch_id>
python -m prod.batch.cli report <batch_id>     # builds + writes report.json + report.md

# Cancel a running batch (propagates to children)
python -m prod.batch.cli cancel <batch_id>

# Force ASGs to zero (use if `run` exited without cleanup)
python -m prod.batch.cli teardown-fleet
```

`run` is the primary path; `submit --wait` is useful when the fleet is
already up (re-runs, debugging). Without `--wait`, `submit` prints the
workflow ID — track progress via `status` or the Temporal UI.

## Testing

**Unit tests** (no infra dependencies):

```bash
python -m tests.test_batch_planner       # 10 tests: sharding, ID format, validation
python -m tests.test_batch_workflows     # 14 tests: pure helpers (merge_config, rows, truncate)
```

**Integration smoke test** (requires the local docker-compose Temporal stack
plus a running worker plus a reachable vLLM endpoint):

```bash
# In one terminal:
make infra && make worker

# In another:
python -m tests.integration.test_batch_e2e
```

The integration test submits a 1-PDF manifest pointing at `etl/hybrid.pdf`,
runs it end-to-end through `BatchRunWorkflow → ShardWorkflow → ProcessPdfWorkflow`,
and asserts the report files are written. It skips politely if Temporal or
vLLM is unavailable.

## Phasing

See `BATCH_PROCESSING_PLAN.md` at the repo root for the multi-phase build
plan. Phases 3-4 (skeleton + workflows) are now in place; phases 5-6
(autoscaling fleet + batch-isolated vLLM endpoint) are still pending.
