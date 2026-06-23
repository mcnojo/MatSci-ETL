# prod/ — AWS deployment

Two motifs, one workflow: both ultimately submit `ProcessPdfWorkflow`
(`prod/live/workflows/process_pdf.py`) on `cpu-task-queue` + `gpu-task-queue`.

| Subpackage      | Trigger                | Optimizes for          |
| --------------- | ---------------------- | ---------------------- |
| `prod/live/`    | SQS (always-on)        | per-PDF latency        |
| `prod/batch/`   | `cli submit` (bounded) | GPU utilization        |
| `prod/reports/` | walker over Temporal   | batch/live/comparison  |

Pipeline logic lives in `etl/pipeline/`; `prod/` is deployment orchestration.
Shared Temporal infra (task queues, retry policies, activity I/O, client) lives
in `shared/temporal/`.

Both lanes share the pipeline config overlay at `prod/live/config/prod_config.yaml`
(batch's `cli submit` defaults `--prod-overlay` to it), so finalized
`tree.json` lands at `s3://chem-lit-artifacts/trees/<document_id>/tree.json`
for both. Trees sit outside the 3-day `assets/` lifecycle rule; fetch with
`bin/pull-trees.sh`.

See `prod/live/README.md`, `prod/batch/README.md`, `prod/reports/README.md`.
