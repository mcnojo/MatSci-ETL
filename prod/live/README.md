# prod/live/ — streaming SQS → Temporal service

Long-running service that processes PDFs as they land in S3. Optimizes for
per-PDF latency.

```
S3 live/incoming -> SQS -> ingestion consumer -> Temporal ProcessPdfWorkflow
                                                  ├─ CPU activities (cpu-task-queue)
                                                  └─ GPU activities (gpu-task-queue)
                                              -> S3 artifacts
```

Two EC2 hosts (terraform-managed via `shared/temporal/terraform/` and `shared/vllm/terraform/`):

| Host            | Type       | Role                                               |
| --------------- | ---------- | -------------------------------------------------- |
| cpu-pipeline-01 | m7i.xlarge | Temporal + Postgres (Docker), worker, SQS consumer |
| vLLM box        | g6.xlarge  | vLLM model server (chandra)                        |

For the bulk-job/batch path see `prod/batch/`.

## Local development

```bash
# 1. Start Temporal + Postgres
make infra          # docker compose up -d

# 2. Start worker (handles both task queues)
make worker         # python -m prod.live.worker

# 3. Run a workflow
python -m etl.cli --pdf etl/hybrid.pdf

# 4. Temporal UI at http://localhost:8233
```

## Prod setup

```bash
bin/live/up.sh             # terraform: shared/temporal + shared/vllm + live
bin/live/submit.sh <pdf>…  # uploads to live/incoming/, S3 fires SQS → consumer
bin/live/down.sh
```

`bin/live/up.sh` brings up everything: cpu-pipeline-01 (which runs the worker
+ ingestion systemd units), the vLLM box, and the SQS queue + S3 notification.

## SQS queue URL handoff

The SQS queue URL is published as SSM parameter `/ocr-bench/live/queue_url`
by `prod/live/terraform/`. The `ocr-ingestion` systemd unit fetches it at
startup (`ExecStartPre`) and injects it as `OCR_LIVE_QUEUE_URL`. The
consumer honors this env var over the value in `prod_config.yaml`, so the
config file stays clean.
