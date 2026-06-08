# prod/batch/ — bounded bulk-job processor

Counterpart to `prod/live/`. Optimizes for **GPU utilization across a full
corpus** (saturate the fleet, then tear it down) rather than per-PDF
latency.

## Layout

```
prod/batch/
├── cli.py                              # operator entry: submit / status / cancel / report / wait-for-workers
├── planner.py                          # manifest → shards (pure)
├── artifacts.py                        # S3 manifest read, report write
├── models.py                           # BatchManifest, BatchItem
├── reports/                            # end-of-batch hardware + workflow report
├── workflows/
│   ├── batch_run.py                    # BatchRunWorkflow (parent — lifecycle + fan out + report)
│   ├── shard.py                        # ShardWorkflow (child — ~50 PDFs)
│   ├── models.py                       # BatchRunInput/Output, ShardInput/Output, ItemResult
│   └── activities/                     # per-stage activities
│       ├── fetch_manifest.py
│       ├── write_report.py
│       ├── scale_fleet.py              # scale_fleet_up + scale_fleet_down
│       ├── await_pollers.py            # blocks until workers register
│       └── build_report.py             # Temporal + CloudWatch → report.json/md
├── config/
│   └── batch_config.yaml
└── scripts/
    └── user_data.sh.tpl                # worker bootstrap (rendered by terraform)
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
# Start a batch — workflow owns the full lifecycle (scale up, await pollers,
# fan out, write reports, scale down). Default waits for completion.
python -m prod.batch.cli submit --manifest s3://chem-lit-artifacts/batches/q2-corpus-a/manifest.json

# Submit against a pre-existing fleet (local dev / debug runs).
python -m prod.batch.cli submit --manifest /path/to/manifest.json --no-manage-fleet

# Inspect
python -m prod.batch.cli status <batch_id>
python -m prod.batch.cli report <batch_id>     # re-runs the rich report build

# Cancel a running batch (propagates to children; finally block scales down)
python -m prod.batch.cli cancel <batch_id>

# Diagnostic: wait until activity pollers register on the named queues
python -m prod.batch.cli wait-for-workers
```

The Phase D Lambda is the production trigger (S3 PUT on manifest.json fires
it). `cli submit` is kept for power users / debug runs where bypassing the
Lambda is useful.

## Testing

**Unit tests** (no infra dependencies):

```bash
python -m tests.test_batch_planner       # 10 tests: sharding, ID format, validation
python -m tests.test_batch_workflows     # pure helpers (merge_config, rows, truncate)
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
runs it end-to-end through `BatchRunWorkflow → ShardWorkflow → ProcessPdfWorkflow`
with `--no-manage-fleet` semantics (fleet field unset), and asserts the
summary report files are written. It skips politely if Temporal or vLLM is
unavailable.

## Phasing

See `AWS_DEPLOYMENT_PLAN.md` at the repo root for the deployment plan.
Phase C (this commit) moves the batch lifecycle into `BatchRunWorkflow`.
Phase D adds the Lambda trigger; Phase E ships the operator-facing
`bin/` scripts.
