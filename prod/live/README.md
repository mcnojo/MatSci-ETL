# prod/live/ — SQS-driven single-PDF service

Long-running service. Optimizes per-PDF latency. For bulk jobs see `prod/batch/`.

```
S3 live/incoming/ -> SQS -> ingestion/consumer.py -> ProcessPdfWorkflow
  cpu-task-queue  CPU activities (load_pages, extract_assets, attach_ocr, assign_elements, finalize)
  gpu-task-queue  GPU activities (llm_text_call, chandra_vision_call)
-> s3://chem-lit-artifacts/trees/<document_id>/tree.json
```

Workflow defined in `workflows/process_pdf.py` (shared with batch — task queue is
the motif signal). Activities live in `pipeline/{cpu,gpu}_activities.py`.

Hosts (terraform: `shared/temporal`, `shared/vllm`, `prod/live/terraform`):

| Host            | Type        | Role                                                |
| --------------- | ----------- | --------------------------------------------------- |
| cpu-pipeline-01 | m7i.xlarge  | Temporal + Postgres (Docker), worker, SQS consumer  |
| vllm-chandra    | g6.xlarge   | chandra OCR (L4 24GB)                               |
| vllm-gemma      | g6e.xlarge  | gemma tree_llm (L40S 48GB, 128K ctx)                |

Worker concurrency: 8 CPU / 4 GPU activities (`config/prod_config.yaml`).

## Usage

```bash
bin/live/up.sh             # applies shared/platform, shared/temporal, shared/vllm, live
bin/live/submit.sh <pdf>…  # S3 upload -> SQS -> consumer auto-starts workflow
bin/live/down.sh
bin/pull-trees.sh [dest]   # syncs s3://<bucket>/trees/*/tree.json (live + batch share kb_root)
```

For one-off runs from the operator's Mac against the running AWS Temporal:

```bash
python -m pipeline.cli --pdf paper.pdf
```

## Wiring notes

- **SQS URL handoff.** `prod/live/terraform` publishes the queue URL to SSM
  `/ocr-bench/live/queue_url`. The `ocr-ingestion` systemd unit reads it in
  `ExecStartPre` and exports `OCR_LIVE_QUEUE_URL`; the consumer prefers env
  over `ingestion.queue_url` in the config.
- **vLLM resolution.** `tree_llm.base_url` and `vision_server.base_url` use
  `vllm-instance://<role>:<port>/v1` and resolve at activity boundary via
  EC2 tag lookup (`shared/vllm/resolve.py`, driven by
  `OCR_VLLM_PREFER_PRIVATE_IP` in the worker systemd unit).
- **Artifact layout.** `s3://chem-lit-artifacts/assets/` for element/page PNGs
  (3-day lifecycle); `s3://chem-lit-artifacts/trees/` for final tree.json (no
  lifecycle). `finalize_activity` accepts both `s3://` and local output paths.
- **Workflow ID.** `process-pdf-<document_id>-<run_id>`. `document_id` is the
  sanitized PDF stem (`ingestion/consumer.py::derive_document_id`).
