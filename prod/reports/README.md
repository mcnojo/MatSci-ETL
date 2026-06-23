# `prod/reports`

Three report shapes, one walker pipeline. Each drops `report.json` (Pydantic dump)
+ `report.md` (human) under `{root}` from `prod/batch/config/batch_config.yaml::report.s3_root`
(prod: `s3://chem-lit-artifacts`).

| Report     | Discovery                                       | S3 path                                |
| ---------- | ----------------------------------------------- | -------------------------------------- |
| Batch      | walk from `BatchRunWorkflow` ID + children      | `{root}/batches/{batch_id}/report/`    |
| Live       | Temporal visibility query, closed `ProcessPdfWorkflow` by `CloseTime` | `{root}/live/reports/{since}_{until}/` |
| Comparison | pure combinator over one batch + one live       | `{root}/comparisons/{utc_iso}/`        |

## Contents

**Batch / Live** (same shape, different scope):
- Items: total / succeeded / failed (from `ProcessPdfWorkflow` records)
- Workflows per type: count, success, fail, duration p50/p95/max
- Activities per type: count, success, fail, retries, start->close + schedule->close latency
- Hardware per instance (CWAgent -> `OCR/Batch/Worker` | `OCR/Live/Worker`):
CPU active %, memory %, worker CPU %, worker RSS, net bytes/sec
- GPU per vLLM instance (nvidia-smi sidecar -> `OCR/vLLM/GPU`): util %, memory MiB, total VRAM
- Flags: failures, retries, p99/p50 > 5x, CPU p95 > 95%, memory max > 90%

**Comparison**: two-column "Live | Batch" with delta + ratio for items/throughput,
`ProcessPdfWorkflow` p50/p95/p99/max, per-activity p50+p95 (left-outer on `activity_type`),
hardware aggregates (avg of per-instance p95s — instance counts differ), GPU.
The GPU row is the headline: batch should saturate (≥90% p95), live should not.

## CLI

```bash
python -m prod.reports batch <batch_id>
python -m prod.reports live --since 24h
python -m prod.reports live --since 30m --until 5m       # closed window
python -m prod.reports compare --batch <id> --live-window 24h
```

Group flags: `--temporal-address` (resolves: flag -> `TEMPORAL_ADDRESS` env ->
terraform `cpu_pipeline_public_ip:7233` -> `localhost:7233`),
`--temporal-namespace` (`default`), `--batch-config`
(`prod/batch/config/batch_config.yaml`), `--region` (`us-west-2`).

Per-command: `--region`, `--out` (override `report.s3_root`), `--skip-hardware`.

## When to run

- **End-of-batch**: `BatchRunWorkflow` writes the batch report itself via
  `build_report_activity`. Re-run `batch` only if that activity failed.
- **Live regression**: short window, e.g. `--since 1h --until 0s`.
- **Capacity / where-time-goes**: `compare` — GPU + per-activity p50 show
  which stages run hot in batch but idle in live (the win) vs hot in both.

`compare` reads the cached batch report from S3 when present; rebuilds otherwise.

## Scheduled live reports

`cpu-pipeline-01` runs `python -m prod.reports live --since 24h` daily via
`ocr-live-report.timer` (wired in `shared/temporal/terraform/cpu_pipeline.tf`).
Output -> `s3://<artifact-bucket>/live/reports/{yesterday_iso}_{today_iso}/`;
log -> `/var/log/ocr-live-report.log` (mirrored to CW). Keeps a fresh 24h
snapshot available so `compare --live-window 24h` always has data.

## Notes

- `temporal_walker.py` is shared; batch vs live differs only in workflow-set discovery.
- `hardware.py` parameterizes on namespace — same query against batch ASGs,
  cpu-pipeline-01, and the vLLM sidecar.
- `_pick_period` matches CW retention: 60s for ≤15d, 300s for ≤63d, 3600s beyond.
