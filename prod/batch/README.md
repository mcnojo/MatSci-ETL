# prod/batch/ — bounded bulk-job processor

Counterpart to `prod/live/`. Optimizes for **GPU utilization across a full
corpus** (saturate the fleet, then tear it down) rather than per-PDF
latency.

## Layout

```
prod/batch/
├── cli.py                 # operator entry: submit / status / cancel / report
├── planner.py             # manifest → shards (pure)
├── artifacts.py           # S3 manifest read, report write
├── models.py              # BatchManifest, BatchItem
├── activities.py          # fetch_manifest_activity, write_report_activity
├── workflows/
│   ├── batch_run.py       # BatchRunWorkflow (parent — fan out + report)
│   └── shard.py           # ShardWorkflow (child — ~50 PDFs)
├── config/
│   └── batch_config.yaml
└── scripts/
    ├── submit_batch.sh
    └── teardown_batch.sh
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
python -m prod.batch.cli submit --manifest s3://chem-lit-artifacts/batches/q2-corpus-a/manifest.json
python -m prod.batch.cli status <batch_id>
python -m prod.batch.cli cancel <batch_id>
python -m prod.batch.cli report <batch_id>
```

`submit` is the only command implemented in Phase 3 (dry-run: parses the
manifest, prints the shard plan, exits). Phase 4 wires it to start
`BatchRunWorkflow`.

## Phasing

See `BATCH_PROCESSING_PLAN.md` at the repo root for the multi-phase build
plan. This subpackage tracks phases 3-6 (skeleton, workflows, autoscaling
fleet, batch-isolated vLLM endpoint).
