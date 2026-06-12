# `prod/reports`

Three report shapes, one walker pipeline:

| Report      | Walker                        | Bounded by                          | S3 path                                          |
| ----------- | ----------------------------- | ----------------------------------- | ------------------------------------------------ |
| Batch       | `walk_batch(batch_wf_id)`     | one `BatchRunWorkflow` + children   | `{root}/batches/{batch_id}/report/`              |
| Live        | `walk_live_window(since,until)` | rolling window over `ProcessPdfWorkflow` | `{root}/live/reports/{since}_{until}/`     |
| Comparison  | (pure combinator)             | one batch + one live, joined        | `{root}/comparisons/{utc_iso}/`                  |

All three drop `report.json` (full Pydantic dump) and `report.md` (human).
`{root}` is the bucket root — `s3://chem-lit-artifacts` for prod. Configure
via `prod/batch/config/batch_config.yaml::report.s3_root`.

## What's in each report

### Batch / Live

Same shape, different scope:

- **Items**: total, succeeded, failed (counted from `ProcessPdfWorkflow` records)
- **Workflows**: per workflow_type — count, success, fail, duration p50/p95/max
- **Activities**: per activity_type — count, success, fail, retries,
  start→close latency, schedule→close latency (queue + execute)
- **Hardware** (per instance, from CWAgent → `OCR/Batch/Worker` or `OCR/Live/Worker`):
  CPU active %, memory %, worker-process CPU %, worker RSS, net bytes/sec
- **GPU** (per vLLM instance, from the nvidia-smi sidecar → `OCR/vLLM/GPU`):
  utilization %, memory used MiB, total VRAM
- **Flags**: human-readable anomalies (activity failures, retries, long-tail
  p99/p50 ratios, CPU/memory saturation)

### Comparison

Two-column "Live | Batch" tables with delta + ratio per metric:

- **Items / throughput**: items, error rate, items/hr
- **`ProcessPdfWorkflow` duration**: p50/p95/p99/max
- **Per-activity p50, p95** start→close: joined on `activity_type` (left-outer)
- **Hardware**: CPU %, memory %, worker CPU %, net sent/recv KiB/s, instance count
- **GPU**: utilization %, memory MiB, instance count

The GPU row is the headline metric for this whole exercise — a batch should
saturate the GPU (≥90% p95 utilization) while live should not (single-digit %
utilization between requests). A ~10× ratio there is what justifies the
existence of the batch fleet.

## CLI

```bash
# Batch report (re-run the rich build after the workflow finishes / fails)
python -m prod.reports batch <batch_id>

# Live report — rolling window
python -m prod.reports live --since 24h
python -m prod.reports live --since 30m --until 5m   # closed window: 30m wide, ending 5m ago

# Comparison — uses the cached batch report on S3 when present (falls back to rebuild)
python -m prod.reports compare --batch <batch_id> --live-window 24h
```

Group-level flags (apply to all subcommands):
- `--temporal-address` (default `localhost:7233`)
- `--temporal-namespace` (default `default`)
- `--batch-config <path>` (default `prod/batch/config/batch_config.yaml`) —
  source of the `--out` default (report root)
- `--region <aws-region>` (default `us-west-2`) — default for per-command `--region`

Per-command flags:
- `--region` — AWS region for CloudWatch. Defaults to the group `--region`.
- `--out` — override the bucket root. Defaults to `report.s3_root`.
- `--skip-hardware` — skip the CloudWatch fetch (local dev / fast iteration).

## When to run which

- **End-of-batch**: the `BatchRunWorkflow` writes the batch report itself in
  `build_report_activity`. Re-run via `python -m prod.reports batch` only if
  that activity failed (operator gets a warning in the workflow log).
- **Investigating a live regression**: pull a short window with
  `--since 1h --until 0s` to focus on the last hour.
- **Capacity planning / "where does the time go"**: `compare` against a
  representative recent batch. The headline GPU and per-activity-p50 tables
  show which stages run hot in batch mode but idle in live (good — that's
  the win) and which stay hot in both (worth optimizing).

## Scheduled live reports

cpu-pipeline-01 runs `python -m prod.reports live --since 24h` once a day
via the `ocr-live-report.timer` systemd unit. Output:

- Report files: `s3://<artifact-bucket>/live/reports/{yesterday_iso}_{today_iso}/`
- Service log: `/var/log/ocr-live-report.log` (mirrored to CW)

A recent live snapshot is therefore always available without operator
action, so `compare` can resolve `--live-window 24h` against fresh data.

## Architecture notes

- `temporal_walker.py` is shared between batch and live. The only
  difference is how the workflow set is discovered: batch traverses from a
  root workflow ID; live queries Temporal visibility for closed executions
  by `WorkflowType` + `CloseTime`.
- `hardware.py` parameterizes on `namespace`, so the same query shape works
  against `OCR/Batch/Worker` (batch ASGs), `OCR/Live/Worker`
  (cpu-pipeline-01), and `OCR/vLLM/GPU` (sidecar).
- CW retention curve drives the `_pick_period` heuristic: 1s/10s for 3h,
  60s for 15d, 300s for 63d, 3600s for 15mo. The walker auto-picks based on
  window age so reports built much later still resolve.
- The comparison renderer prefers _aggregated_ hardware metrics ("avg of
  per-instance p95s") over per-instance breakdown, because the two sides
  have very different instance counts (live: 1 cpu-pipeline; batch: 4+
  workers across 2 ASGs). Per-instance detail still lives in the
  individual batch / live reports.
